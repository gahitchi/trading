"""Durable accounting journal: restart, corruption, concurrency, and exact money."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from lab.profit_allocation import AllocationEvent
from lab.profit_journal import ProfitJournal
from tests.test_profit_allocation import START, T0, close, event, policy, trade


def journal(tmp_path, **changes):
    return ProfitJournal(tmp_path / "research-ledger.jsonl", policy(**changes), START)


def _rows(path):
    return [json.loads(s) for s in path.read_text().splitlines()]


def _rewrite(path, rows):
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))


def test_durable_restart_preserves_partial_compounding_and_losses(tmp_path):
    first = journal(tmp_path)
    first.append(trade("d1", "0.20"))
    assert first.append(close("one", START)).net_pnl == 0.20
    second = journal(tmp_path)
    assert second.replay().equity == 1000.20
    second.append(trade("d2", "120.00", minutes=1441))
    second.append(close("two", START + timedelta(days=1)))
    third = journal(tmp_path)
    third.append(trade("d3", "-80.00", minutes=2881))
    third.append(close("three", START + timedelta(days=2)))
    summary = journal(tmp_path).report(START + timedelta(days=2))
    assert summary["net_pnl"] == "40.20"
    assert summary["deployable"] == "980.10"
    assert summary["reserve"] == "60.10"
    assert summary["winning_days"] == 2 and summary["losing_days"] == 1
    assert summary["end_equity"] == "1040.20"
    assert (tmp_path / "research-ledger.jsonl.head.json").is_file()


def test_duplicate_event_and_duplicate_economic_source_rejected_after_restart(tmp_path):
    first = journal(tmp_path)
    first.append(trade("once", "10.00"))
    before = first.path.read_bytes()
    again = journal(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        again.append(trade("once", "10.00"))
    repeated_source = AllocationEvent(
        "event:new-id", "source:once", T0 + timedelta(minutes=2),
        "USD", "trade", "2.00", gross="2.00", fees="0.00",
        slippage="0.00", funding="0.00", released_mark="0.00",
    )
    with pytest.raises(ValueError, match="duplicate"):
        again.append(repeated_source)
    assert again.path.read_bytes() == before


def test_replay_rejects_mutated_record_without_regenerating_integrity_seal(tmp_path):
    j = journal(tmp_path)
    j.append(trade("one", "10.00"))
    rows = _rows(j.path)
    rows[-1]["event"]["amount"] = "100.00"
    _rewrite(j.path, rows)
    with pytest.raises(ValueError, match="integrity digest"):
        journal(tmp_path)
    with pytest.raises(ValueError, match="integrity digest"):
        j.append(trade("two", "2.00", minutes=2))


def test_replay_rejects_reordered_event_records(tmp_path):
    j = journal(tmp_path)
    j.append(trade("first", "2.00"))
    j.append(trade("second", "3.00", minutes=2))
    rows = _rows(j.path)
    rows[-2], rows[-1] = rows[-1], rows[-2]
    _rewrite(j.path, rows)
    with pytest.raises(ValueError, match="sequence"):
        j.replay()


def test_truncated_last_record_blocks_further_append_and_does_not_reset(tmp_path):
    j = journal(tmp_path)
    j.append(trade("saved", "12.00"))
    with j.path.open("ab") as stream:
        stream.write(b'{"version":1')
    with pytest.raises(ValueError, match="incomplete"):
        j.replay()
    with pytest.raises(ValueError, match="incomplete"):
        j.append(trade("later", "3.00", minutes=2))
    assert j.path.read_bytes().endswith(b'{"version":1')


def test_truncating_complete_last_entry_is_detected_by_durable_checkpoint(tmp_path):
    j = journal(tmp_path)
    j.append(trade("first", "2.00"))
    j.append(trade("second", "3.00", minutes=2))
    rows = _rows(j.path)
    _rewrite(j.path, rows[:-1])
    with pytest.raises(ValueError, match="journal/checkpoint mismatch"):
        journal(tmp_path)
    with pytest.raises(ValueError, match="journal/checkpoint mismatch"):
        j.append(trade("third", "10.00", minutes=3))


def test_journal_missing_but_checkpoint_present_fails_closed(tmp_path):
    j = journal(tmp_path)
    j.append(trade("one", "2.00"))
    j.path.unlink()
    with pytest.raises(ValueError, match="journal is missing"):
        journal(tmp_path)
    assert not j.path.exists()


def test_checkpoint_missing_refuses_append_and_reopen(tmp_path):
    j = journal(tmp_path)
    j.append(trade("once", "1.00"))
    head = tmp_path / "research-ledger.jsonl.head.json"
    head.unlink()
    with pytest.raises(ValueError, match="checkpoint is missing"):
        journal(tmp_path)
    with pytest.raises(ValueError, match="checkpoint is missing"):
        j.append(trade("again", "2.00", minutes=2))


def test_checkpoint_edit_or_wrong_last_digest_stops_all_writes(tmp_path):
    j = journal(tmp_path)
    j.append(trade("once", "1.00"))
    head = tmp_path / "research-ledger.jsonl.head.json"
    raw = json.loads(head.read_text())
    raw["last_sha256"] = "a" * 64
    head.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="integrity digest"):
        j.replay()


def test_opening_journal_with_changed_policy_or_day_is_forbidden(tmp_path):
    journal(tmp_path)
    with pytest.raises(ValueError, match="different frozen policy"):
        journal(tmp_path, reinvest_fraction="1")
    with pytest.raises(ValueError, match="different frozen policy"):
        ProfitJournal(tmp_path / "research-ledger.jsonl", policy(), START + timedelta(days=1))


def test_invalid_economics_stays_off_disk_and_can_be_retried_correctly(tmp_path):
    j = journal(tmp_path)
    original = j.path.read_bytes()
    with pytest.raises(ValueError, match="trade net"):
        j.append(trade("same", "10.00", gross="20.00", fees="1.00"))
    assert j.path.read_bytes() == original
    j.append(trade("same", "19.00", gross="20.00", fees="1.00"))
    assert j.replay().equity == 1019.00


def test_missing_daily_close_rejected_before_record_is_written(tmp_path):
    j = journal(tmp_path)
    j.append(trade("today", "2.00"))
    before = j.path.read_bytes()
    with pytest.raises(ValueError, match="missing UTC day-close"):
        j.append(trade("tomorrow", "3.00", minutes=1441))
    assert j.path.read_bytes() == before
    j.append(close("d1", START))
    j.append(trade("tomorrow", "3.00", minutes=1441))
    j.append(close("d2", START + timedelta(days=1)))
    assert j.report(START + timedelta(days=1))["net_pnl"] == "5.00"


def test_external_flows_and_reserve_transfers_do_not_inflate_daily_profit(tmp_path):
    j = journal(tmp_path, reinvest_fraction="0")
    j.append(trade("gain", "120.00"))
    j.append(event("to-deployable", "reserve_transfer", "40.00", minutes=2))
    j.append(event("withdraw", "external_flow", "-30.00", minutes=3))
    day = j.append(close("d1", START))
    assert day.net_pnl == 120.00 and day.external_flows == -30.00
    assert j.replay().equity == 1090.00
    assert j.report(START)["deployable"] == "1010.00"
    assert j.report(START)["reserve"] == "80.00"


def test_cross_midnight_mark_and_release_reconciles_after_restart(tmp_path):
    j = journal(tmp_path)
    j.append(event("mark1", "mark", "10.00"))
    j.append(close("d1", START, mark="10.00"))
    j = journal(tmp_path)
    j.append(trade("exit", "8.00", released_mark="10.00", minutes=1441))
    d2 = j.append(close("d2", START + timedelta(days=1)))
    assert d2.net_pnl == -2.00
    assert j.report(START + timedelta(days=1))["net_pnl"] == "8.00"


def test_competing_writers_are_serialized_and_economics_preserved(tmp_path):
    j = journal(tmp_path)

    def publish(i):
        local = journal(tmp_path)
        return local.append(trade(f"t{i}", "0.01"))

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert len(list(pool.map(publish, range(25)))) == 25
    after = journal(tmp_path)
    assert len(_rows(after.path)) == 26
    assert after.replay().equity == 1000.25
    after.append(close("end", START))
    assert after.report(START)["net_pnl"] == "0.25"


def test_append_fsync_failure_does_not_claim_a_settlement(tmp_path, monkeypatch):
    import lab.profit_journal as mod

    j = journal(tmp_path)
    before = j.path.read_bytes()
    original = mod._write_all

    def fail_before_bytes(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_all", fail_before_bytes)
    with pytest.raises(OSError, match="disk full"):
        j.append(trade("attempt", "5.00"))
    assert j.path.read_bytes() == before
    monkeypatch.setattr(mod, "_write_all", original)
    j.append(trade("attempt", "5.00"))
    assert j.replay().equity == 1005.00


def test_torn_append_failure_blocks_recovery_without_manual_evidence_audit(tmp_path, monkeypatch):
    import lab.profit_journal as mod

    j = journal(tmp_path)
    original = mod._write_all

    def torn(fd, raw):
        os.write(fd, raw[:10])
        raise OSError("I/O error")

    monkeypatch.setattr(mod, "_write_all", torn)
    with pytest.raises(OSError, match="I/O error"):
        j.append(trade("incomplete", "5.00"))
    monkeypatch.setattr(mod, "_write_all", original)
    with pytest.raises(ValueError, match="incomplete"):
        j.replay()
    with pytest.raises(ValueError, match="incomplete"):
        j.append(trade("next", "5.00"))


def test_checkpoint_publication_failure_blocks_retry_instead_of_double_booking(tmp_path, monkeypatch):
    import lab.profit_journal as mod

    j = journal(tmp_path)
    original = mod.atomic_json

    def cannot_commit(*_args):
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(mod, "atomic_json", cannot_commit)
    with pytest.raises(OSError, match="checkpoint unavailable"):
        j.append(trade("committed-to-events", "5.00"))
    monkeypatch.setattr(mod, "atomic_json", original)
    with pytest.raises(ValueError, match="journal/checkpoint mismatch"):
        j.replay()
    with pytest.raises(ValueError, match="journal/checkpoint mismatch"):
        j.append(trade("repeat", "5.00"))


def test_malformed_input_does_not_publish_or_erase_journal(tmp_path):
    j = journal(tmp_path)
    data = j.path.read_bytes()
    with pytest.raises(ValueError, match="currency mismatch"):
        j.append(AllocationEvent(
            "other", "other-source", T0, "EUR", "mark", "0.00"
        ))
    assert j.path.read_bytes() == data


def test_journal_is_not_a_trading_order_router(tmp_path):
    j = journal(tmp_path)
    assert not hasattr(j, "place_order")
    assert not hasattr(j, "connect_exchange")
    assert j.replay().risk_capacity == 1000.00
