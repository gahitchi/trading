"""Portable, conservative paper account snapshot invariants and durability."""

from dataclasses import replace

import pytest

import lab.paper_account_snapshot as module
from lab.paper_account_snapshot import (
    PaperAccountSnapshot,
    PaperAccountSnapshotJournal,
    validate_snapshot,
)
from lab.sandbox_capture import PaperOrderEventCapture
from tests.test_sandbox_capture import IID, NS, event


def capture(tmp_path):
    return PaperOrderEventCapture(tmp_path / "orders.jsonl", session_id="session-1", instrument_id=IID)


def snapshot(source, *, identifier="snapshot-1", captured_ns=None, count=None, **kw):
    if count is None:
        count = len(source.receipts())
    _receipt, digest = source.evidence_prefix(count)
    data = dict(
        snapshot_id=identifier,
        source_id=f"cache:{identifier}",
        source_kind="sandbox-cache",
        account_id="sandbox-1",
        captured_ns=captured_ns or NS + 10,
        capture_count=count,
        capture_sha256=digest,
        started_flat=True,
        exclusive_scope=True,
        complete_snapshot=True,
        positions={IID: "-0.25"},
        open_orders={},
        balances={"USDT": {"total": "1000.00", "available": "990.00", "locked": "10.00"}},
    )
    data.update(kw)
    return PaperAccountSnapshot(**data)


def journal(tmp_path, source):
    return PaperAccountSnapshotJournal(tmp_path / "account.jsonl", source, account_id="sandbox-1")


def test_snapshot_report_checks_fill_delta_and_cash_but_denies_risk_qualification(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    state = snapshot(source)
    report = validate_snapshot(source, state, "sandbox-1")
    assert report["positions_match"] is True
    assert report["signed_quantity"] == "-0.25"
    assert report["cash_arithmetic_ok"] is True
    assert report["source_authenticated"] is False
    assert report["risk_ready"] is False
    assert report["equity_reconciled"] is False
    assert report["order_state_complete"] is False


def test_durable_archive_restart_and_identical_replay(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    state = snapshot(source)
    store = journal(tmp_path, source)
    assert store.append(state)["positions_match"]
    reopened = journal(tmp_path, capture(tmp_path))
    assert reopened.snapshots() == (state,)
    assert reopened.latest_report()["signed_quantity"] == "-0.25"


def test_historical_snapshot_remains_bound_after_new_fills(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    store = journal(tmp_path, source)
    first = snapshot(source, captured_ns=NS + 4)
    store.append(first)
    source.append(
        event(event_id="evt-2", client_order_id="CO-2", trade_id="trade-2", order_side="BUY", last_qty="0.10"),
        received_ns=NS + 6,
    )
    second = snapshot(
        source, identifier="snapshot-2", captured_ns=NS + 8, positions={IID: "-0.15"},
    )
    store.append(second)
    restarted = journal(tmp_path, source)
    assert len(restarted.snapshots()) == 2
    assert restarted.latest_report()["signed_quantity"] == "-0.15"
    assert first.capture_sha256 != second.capture_sha256


def test_missing_event_from_declared_snapshot_prefix_is_blocked(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    first = snapshot(source, captured_ns=NS + 4)
    source.append(
        event(event_id="evt-2", trade_id="trade-2", last_qty="0.10"),
        received_ns=NS + 3,
    )
    with pytest.raises(ValueError, match="omitted"):
        validate_snapshot(source, first, "sandbox-1")


def test_forged_source_prefix_digest_and_count_rejected(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    state = snapshot(source)
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_snapshot(source, replace(state, capture_sha256="0" * 64), "sandbox-1")
    with pytest.raises(ValueError, match="outside committed"):
        validate_snapshot(source, replace(state, capture_count=9), "sandbox-1")


def test_signed_position_mismatch_and_wrong_source_account_rejected(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    state = snapshot(source)
    with pytest.raises(ValueError, match="signed position"):
        validate_snapshot(source, replace(state, positions={IID: "0.25"}), "sandbox-1")
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_snapshot(source, state, "different-account")
    with pytest.raises(ValueError, match="fill account"):
        validate_snapshot(source, replace(state, account_id="different-account"), "different-account")


@pytest.mark.parametrize("field", ["started_flat", "exclusive_scope", "complete_snapshot"])
def test_unproven_scope_cannot_pass_position_check(tmp_path, field):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    state = replace(snapshot(source), **{field: False})
    with pytest.raises(ValueError, match="assertions required"):
        validate_snapshot(source, state, "sandbox-1")


def test_duplicate_or_voided_fill_blocks_snapshot(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    source.append(event(event_id="evt-2", last_px="110"), received_ns=NS + 3)
    state = snapshot(source, captured_ns=NS + 4, positions={IID: "-0.50"})
    with pytest.raises(ValueError, match="duplicate or restated"):
        validate_snapshot(source, state, "sandbox-1")
    other = capture(tmp_path / "different")
    other.append(event(), received_ns=NS + 2)
    other.append(event("OrderFillVoided", event_id="evt-2"), received_ns=NS + 3)
    with pytest.raises(ValueError, match="voided"):
        validate_snapshot(other, snapshot(other, captured_ns=NS + 4), "sandbox-1")


@pytest.mark.parametrize("balances", [
    {"USDT": {"total": "1000.00", "available": "991.00", "locked": "10.00"}},
    {"USDT": {"total": "1000.00", "available": "NaN", "locked": "0.00"}},
    {"USDT": {"total": "1000.00", "available": "-1.00", "locked": "1001.00"}},
    {"USDT": {"total": "1000.00", "available": "990.00"}},
    {},
])
def test_incomplete_or_nonconserving_cash_denied(tmp_path, balances):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    with pytest.raises(ValueError, match=r"cash balance|cash balances"):
        validate_snapshot(source, snapshot(source, balances=balances), "sandbox-1")


def test_open_order_requires_known_nonterminal_client_order(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    with pytest.raises(ValueError, match="absent from captured"):
        validate_snapshot(source, snapshot(source, open_orders={"unseen": "0.1"}), "sandbox-1")
    # A source order is known, but its remaining quantity is only a claim,
    # not independently verified from the current event schema.
    report = validate_snapshot(source, snapshot(source, open_orders={"CO-1": "0.1"}), "sandbox-1")
    assert not report["order_state_complete"]
    source.append(event("OrderCanceled", event_id="evt-2"), received_ns=NS + 12)
    new = snapshot(source, captured_ns=NS + 15, open_orders={"CO-1": "0.1"})
    with pytest.raises(ValueError, match="terminally"):
        validate_snapshot(source, new, "sandbox-1")


def test_snapshot_duplicate_ids_and_backdated_capture_are_denied(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    store = journal(tmp_path, source)
    state = snapshot(source, captured_ns=NS + 5)
    store.append(state)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        store.append(state)
    with pytest.raises(ValueError, match="out-of-order"):
        store.append(snapshot(source, identifier="snapshot-2", captured_ns=NS + 4))
    assert store.path.read_bytes() == before


def test_snapshot_archive_tampering_and_truncated_suffix_block_reads(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    store = journal(tmp_path, source)
    store.append(snapshot(source))
    original = store.path.read_bytes()
    store.path.write_bytes(original.replace(b"990.00", b"991.00"))
    with pytest.raises(ValueError, match="digest mismatch"):
        store.snapshots()
    store.path.write_bytes(original.split(b"\n")[0] + b"\n")
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        store.snapshots()


def test_snapshot_checkpoint_failure_requires_external_recovery(tmp_path, monkeypatch):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    store = journal(tmp_path, source)

    def broken(*args, **kw):
        raise OSError("fsync unavailable")

    monkeypatch.setattr(module, "atomic_json", broken)
    with pytest.raises(OSError, match="fsync unavailable"):
        store.append(snapshot(source))
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        store.snapshots()


def test_snapshot_source_receipt_mutation_invalidates_older_snapshot(tmp_path):
    source = capture(tmp_path)
    source.append(event(), received_ns=NS + 2)
    store = journal(tmp_path, source)
    store.append(snapshot(source))
    source.path.write_bytes(source.path.read_bytes().replace(b"trade-1", b"trade-X"))
    with pytest.raises(ValueError, match="hash mismatch"):
        store.snapshots()


def test_snapshot_missing_checkpoint_and_symbolic_link_refused(tmp_path):
    source = capture(tmp_path)
    store = journal(tmp_path, source)
    store._head.unlink()
    with pytest.raises(ValueError, match="checkpoint missing"):
        store.snapshots()
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(store.path)
    with pytest.raises(ValueError, match="symlinks"):
        PaperAccountSnapshotJournal(alias, source, account_id="sandbox-1")


def test_zero_fill_flat_start_snapshot_is_recorded_but_not_account_authenticated(tmp_path):
    source = capture(tmp_path)
    state = snapshot(source, positions={}, captured_ns=NS + 3)
    store = journal(tmp_path, source)
    out = store.append(state)
    assert out["signed_quantity"] == "0"
    assert out["risk_ready"] is False
    assert out["source_authenticated"] is False
