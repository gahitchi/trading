"""Evidence archive must fail closed on damaged receipts or interrupted checkpoints."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

import lab.paper_evidence as evidence
from lab.paper_evidence import PaperEvidenceArchive, paper_report
from lab.position_reconcile import CashAdjustment, MarkQuote, PositionMark, SimulatedFill
from lab.profit_allocation import AllocationPolicy
from lab.profit_journal import ProfitJournal

DAY = date(2026, 11, 5)
TIME = datetime(2026, 11, 5, tzinfo=UTC)


def policy():
    return AllocationPolicy(
        currency="USD",
        initial_capital="1000.00",
        quantum="0.01",
        reinvest_fraction="0.5",
        max_deployable="1500.00",
    )


def setup(tmp_path):
    archive = PaperEvidenceArchive(tmp_path / "source.jsonl", policy(), DAY)
    journal = ProfitJournal(tmp_path / "profit.jsonl", policy(), DAY)
    return archive, journal


def report(event, receipt=None, captured_at=None):
    return paper_report(
        event,
        origin="nautilus-sandbox",
        captured_at=captured_at or event.timestamp + timedelta(seconds=1),
        raw_receipt=receipt or {"source_id": event.source_id, "sandbox_order": "order-1"},
    )


def open_short():
    timestamp = TIME + timedelta(minutes=1)
    return SimulatedFill(
        "fill:open",
        timestamp,
        "BTCUSDT",
        "SELL",
        "2",
        "100",
        "0.10",
        "0",
        {"BTCUSDT": MarkQuote("100", timestamp, "quote:open")},
    )


def close_short():
    timestamp = TIME + timedelta(minutes=2)
    return SimulatedFill("fill:close", timestamp, "BTCUSDT", "BUY", "2", "98", "0.20", "0", {})


def day_close():
    return PositionMark("mark:day", TIME + timedelta(days=1), {}, True)


def test_archive_preserves_raw_receipts_and_reconciles_net_short_profit(tmp_path):
    archive, journal = setup(tmp_path)
    events = [open_short(), close_short(), day_close()]
    for event in events:
        archive.append(report(event))
    assert len(archive.receipts()) == 3
    assert archive.receipts()[0]["raw_receipt"]["sandbox_order"] == "order-1"
    assert archive.reconcile(journal) == {}
    assert journal.report(DAY)["net_pnl"] == "3.70"
    assert len(journal.events()) == 5
    assert archive.reconcile(journal) == {}
    assert len(journal.events()) == 5


def test_restart_with_unposted_evidence_suffix_is_replayed_once(tmp_path):
    archive, journal = setup(tmp_path)
    archive.append(report(open_short()))
    archive.reconcile(journal)
    assert len(journal.events()) == 2
    archive.append(report(close_short()))
    archive.append(report(day_close()))
    restarted = PaperEvidenceArchive(archive.path, policy(), DAY)
    assert len(restarted.source_events()) == 3
    assert restarted.reconcile(ProfitJournal(journal.path, policy(), DAY)) == {}
    assert journal.report(DAY)["net_pnl"] == "3.70"
    assert len(journal.events()) == 5


def test_duplicate_source_refused_without_mutation(tmp_path):
    archive, _ = setup(tmp_path)
    first = report(open_short())
    archive.append(first)
    before = archive.path.read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        archive.append(first)
    assert archive.path.read_bytes() == before


def test_raw_receipt_source_disagreement_rejected(tmp_path):
    archive, _ = setup(tmp_path)
    mismatch = report(open_short(), receipt={"source_id": "not-original"})
    with pytest.raises(ValueError, match="same source event"):
        archive.append(mismatch)
    assert archive.source_events() == ()


def test_invalid_quote_and_future_receipt_fail_before_archive_write(tmp_path):
    archive, _ = setup(tmp_path)
    before = archive.path.read_bytes()
    source = open_short()
    bad_mark = replace(source, marks={"BTCUSDT": MarkQuote("100", source.timestamp - timedelta(seconds=6), "old")})
    with pytest.raises(ValueError, match="stale"):
        archive.append(report(bad_mark))
    with pytest.raises(ValueError, match="cannot precede"):
        archive.append(report(source, captured_at=source.timestamp - timedelta(seconds=1)))
    assert archive.path.read_bytes() == before


def test_changed_original_receipt_changes_archive_evidence_digest(tmp_path):
    archive, _ = setup(tmp_path)
    archive.append(report(open_short()))
    original = archive.path.read_bytes()
    modified = original.replace(b"order-1", b"order-X")
    assert modified != original
    archive.path.write_bytes(modified)
    with pytest.raises(ValueError, match="digest mismatch"):
        archive.source_events()


def test_full_record_truncation_disagrees_with_checkpoint(tmp_path):
    archive, _ = setup(tmp_path)
    archive.append(report(open_short()))
    archive.append(report(close_short()))
    raw = archive.path.read_bytes()
    archive.path.write_bytes(raw[: raw.rfind(b"\n", 0, -1) + 1])
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        archive.source_events()


def test_torn_final_line_is_never_ignored(tmp_path):
    archive, _ = setup(tmp_path)
    archive.append(report(open_short()))
    archive.path.write_bytes(archive.path.read_bytes() + b'{"partial":')
    with pytest.raises(ValueError, match="incomplete or torn"):
        archive.source_events()


def test_missing_checkpoint_and_symlink_refused(tmp_path):
    archive, _ = setup(tmp_path)
    archive._head.unlink()
    with pytest.raises(ValueError, match="checkpoint is missing"):
        archive.source_events()
    with pytest.raises(ValueError, match="checkpoint is missing"):
        PaperEvidenceArchive(archive.path, policy(), DAY)
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(archive.path)
    with pytest.raises(ValueError, match="symlinks"):
        PaperEvidenceArchive(alias, policy(), DAY)


def test_checkpoint_io_failure_never_claims_success(tmp_path, monkeypatch):
    archive, _ = setup(tmp_path)

    def failed_head(*_args, **_kwargs):
        raise OSError("disk checkpoint failure")

    monkeypatch.setattr(evidence, "atomic_json", failed_head)
    with pytest.raises(OSError, match="checkpoint failure"):
        archive.append(report(open_short()))
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        archive.source_events()


def test_archive_policy_and_source_cannot_be_reinterpreted(tmp_path):
    archive, journal = setup(tmp_path)
    with pytest.raises(ValueError, match="policy or origin"):
        PaperEvidenceArchive(archive.path, policy(), DAY, origin="offline-replay")
    other_policy = replace(policy(), reinvest_fraction="0.25")
    with pytest.raises(ValueError, match="policy or origin"):
        PaperEvidenceArchive(archive.path, other_policy, DAY)
    different = ProfitJournal(tmp_path / "another.jsonl", other_policy, DAY)
    with pytest.raises(ValueError, match="policies do not match"):
        archive.reconcile(different)
    assert journal.events() == ()


def test_empty_paper_day_preserved_and_funding_adjustment_once(tmp_path):
    archive, journal = setup(tmp_path)
    archive.append(report(CashAdjustment("funding:0", TIME + timedelta(minutes=1), "-0.10", "funding")))
    archive.append(report(day_close()))
    archive.reconcile(journal)
    assert journal.report(DAY)["net_pnl"] == "-0.10"
    assert len(archive.receipts()) == 2


def test_source_not_in_paper_scope_never_admitted(tmp_path):
    with pytest.raises(ValueError, match="simulated paper"):
        PaperEvidenceArchive(tmp_path / "live.jsonl", policy(), DAY, origin="exchange-live")
