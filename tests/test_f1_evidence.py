"""Read-only F1.6 evidence manifest, exact source proof and no qualification bypass."""

import hashlib
import json
import sqlite3

import pytest

from lab.economic_store import EconomicEventStore, EconomicStoreError
from lab.f1_evidence import build_f1_local_evidence, run
from lab.order_lifecycle import OrderLifecycleStore

SHA = "a" * 40


def cash(*, event_id="credit", record_id="cash-r1", time=1):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": record_id,
            "event_id": event_id,
            "timestamp_ns": time,
            "kind": "CAPITAL_IN",
            "source_kind": "BROKER_CASH_ACTIVITY",
            "settlement_status": "SETTLED",
            "currency": "USD",
            "amount": "250",
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()


def order():
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": "order-ack-1",
            "event_id": "accepted-event-1",
            "timestamp_ns": 2,
            "source_kind": "BROKER_ORDER_EVENT",
            "broker_order_id": "order-1",
            "kind": "ACCEPTED",
            "symbol": "SPY",
            "side": "BUY",
            "requested_quantity": "1",
            "cumulative_executed_quantity": "0",
            "remaining_quantity": "1",
            "fill_record_id": None,
            "fill_source_sha256": None,
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()


def test_same_frozen_journal_produces_identical_readonly_manifest_across_restarts(tmp_path):
    path = tmp_path / "economic.sqlite"
    store = EconomicEventStore(path, "SIM-001")
    store.append_cash_record(cash(), source_namespace="broker")
    lifecycle = OrderLifecycleStore(store)
    lifecycle.append(order(), source_namespace="broker", expected_head=store.load().evidence_head)
    ro = EconomicEventStore(path, "SIM-001", readonly=True)
    first = build_f1_local_evidence(ro, declared_commit_sha=SHA)
    second = build_f1_local_evidence(EconomicEventStore(path, "SIM-001", readonly=True), declared_commit_sha=SHA)
    assert first == second
    assert first.sha256 == hashlib.sha256(
        json.dumps(first.manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    report = first.manifest
    assert report["purpose"] == "F1_LOCAL_UNQUALIFIED_REPLAY_EVIDENCE"
    assert report["source_inventory_count"] == 1
    assert report["event_count"] == 1
    assert report["broker_fill_count"] == 0
    assert report["order_event_count"] == 1
    assert report["unresolved_order_ids"] == ["order-1"]
    assert report["rational_projection_matches"] is True
    for field in (
        "broker_source_authenticated",
        "evidence_root_independently_anchored",
        "f1_qualified",
        "release_authorized",
        "execution_authorized",
    ):
        assert report[field] is False
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM economic_events").fetchone()[0] == 1


def test_original_source_corruption_prevents_manifest_without_repair_or_write(tmp_path):
    path = tmp_path / "tampered.sqlite"
    store = EconomicEventStore(path, "SIM-001")
    store.append_cash_record(cash(), source_namespace="broker")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE economic_events SET source_bytes=? WHERE seq=1", (b"not-original",))
    with pytest.raises(EconomicStoreError):
        build_f1_local_evidence(EconomicEventStore(path, "SIM-001", readonly=False), declared_commit_sha=SHA)


def test_missing_broker_order_lifecycle_is_explicitly_not_complete(tmp_path):
    path = tmp_path / "no-order.sqlite"
    store = EconomicEventStore(path, "SIM-001")
    original_fill = json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": "execution-001",
            "event_id": "fill-001",
            "timestamp_ns": 4,
            "kind": "BROKER_FILL",
            "status": "EXECUTED",
            "source_kind": "BROKER_EXECUTION_REPORT",
            "broker_order_id": "order-1",
            "symbol": "SPY",
            "side": "BUY",
            "quote_currency": "USD",
            "commission_currency": "USD",
            "quantity": "1",
            "price": "100",
            "commission": "0",
        },
        sort_keys=True, separators=(",", ":"),
    ).encode()
    store.append_broker_fill_record(original_fill, source_namespace="broker")
    report = build_f1_local_evidence(store, declared_commit_sha=SHA).manifest
    assert report["broker_fill_count"] == 1
    assert report["complete_local_fill_lifecycle_coverage"] is False
    assert report["f1_qualified"] is False


def test_manifest_cli_fails_closed_without_real_existing_journal(tmp_path, capsys):
    rc = run(["--journal", str(tmp_path / "missing.db"), "--account", "SIM-001", "--commit", SHA])
    assert rc == 3
    assert not (tmp_path / "missing.db").exists()
    assert "refused" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["not-a-sha", "A" * 40, "f" * 41, "", "0" * 39])
def test_manifest_rejects_noncanonical_source_revision(tmp_path, bad):
    store = EconomicEventStore(tmp_path / "head.sqlite", "SIM-001")
    with pytest.raises(EconomicStoreError):
        build_f1_local_evidence(store, declared_commit_sha=bad)
