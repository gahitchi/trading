"""F1.4 atomic original order-report custody is offline and non-authorizing."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lab.economic_store import EconomicEventStore, EconomicStoreError


def source_document():
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": "fill-001",
            "event_id": "fill-event-001",
            "timestamp_ns": 20,
            "kind": "BROKER_FILL",
            "status": "EXECUTED",
            "source_kind": "BROKER_EXECUTION_REPORT",
            "broker_order_id": "order-001",
            "symbol": "SPY",
            "side": "BUY",
            "quote_currency": "USD",
            "commission_currency": "USD",
            "quantity": "1",
            "price": "100",
            "commission": "0.01",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def order_document(*, record="order-report-001", digest=None, as_of_ns=30, schema=2):
    order = {
        "broker_order_id": "order-001",
        "symbol": "SPY",
        "side": "BUY",
        "status": "FILLED",
        "requested_quantity": "1",
        "executed_quantity": "1",
        "remaining_quantity": "0",
    }
    if schema == 2:
        order["execution_receipts"] = [
            {
                "record_id": "fill-001",
                "quantity": "1",
                "source_sha256": digest or hashlib.sha256(source_document()).hexdigest(),
            }
        ]
    return json.dumps(
        {
            "schema_version": schema,
            "account_id": "SIM-001",
            "record_id": record,
            "as_of_ns": as_of_ns,
            "source_kind": "BROKER_ORDER_STATEMENT",
            "complete_order_scope": True,
            "orders": [order],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def journal(tmp_path: Path):
    store = EconomicEventStore(tmp_path / "f1.db", "SIM-001")
    store.append_broker_fill_record(source_document(), source_namespace="broker")
    return store


def test_order_source_bytes_commit_atomically_without_mutating_cash_or_positions(tmp_path):
    store = journal(tmp_path)
    before = store.load()
    raw = order_document()
    captured = store.capture_order_observation(raw, source_namespace="broker", expected_head=before.evidence_head)
    assert captured.events_replayed == 1
    assert captured.broker_fills_checked == 1
    assert captured.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert not captured.broker_source_authenticated
    assert store.load() == before
    assert store.inspect_order_observations() == (captured,)
    assert EconomicEventStore(store.path, "SIM-001", readonly=True).inspect_order_observations() == (captured,)


def test_exact_retry_is_idempotent_and_conflicting_same_id_fails_closed(tmp_path):
    store = journal(tmp_path)
    head = store.load().evidence_head
    raw = order_document()
    first = store.capture_order_observation(raw, source_namespace="broker", expected_head=head)
    assert store.capture_order_observation(raw, source_namespace="broker", expected_head=head) == first
    with pytest.raises(EconomicStoreError, match="conflicting"):
        store.capture_order_observation(order_document(as_of_ns=31), source_namespace="broker", expected_head=head)
    assert store.inspect_order_observations() == (first,)


def test_stale_head_and_legacy_order_summaries_cannot_be_archived(tmp_path):
    store = journal(tmp_path)
    with pytest.raises(EconomicStoreError, match="stale"):
        store.capture_order_observation(order_document(), source_namespace="broker", expected_head="0" * 64)
    with pytest.raises(EconomicStoreError, match="v2"):
        store.capture_order_observation(
            order_document(schema=1), source_namespace="broker", expected_head=store.load().evidence_head
        )
    assert store.inspect_order_observations() == ()


def test_missing_journal_receipt_cannot_be_hidden_by_equal_aggregate(tmp_path):
    store = journal(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        store.capture_order_observation(
            order_document(digest="e" * 64),
            source_namespace="broker",
            expected_head=store.load().evidence_head,
        )
    assert store.inspect_order_observations() == ()


def test_archived_snapshot_replays_its_original_prefix_after_new_event(tmp_path):
    from decimal import Decimal

    from lab.engine_events import FillFact

    store = journal(tmp_path)
    raw = order_document()
    old_head = store.load().evidence_head
    first = store.capture_order_observation(raw, source_namespace="broker", expected_head=old_head)
    store.append(FillFact("future-1", "sim:future-1", 40, "QQQ", "BUY", Decimal("1"), Decimal("10"), Decimal("0")))
    assert store.load().evidence_head != old_head
    assert store.inspect_order_observations() == (first,)
    assert store.capture_order_observation(raw, source_namespace="broker", expected_head=old_head) == first


def test_archived_statement_tampering_is_rejected_during_readonly_inspection(tmp_path):
    store = journal(tmp_path)
    store.capture_order_observation(
        order_document(), source_namespace="broker", expected_head=store.load().evidence_head
    )
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE external_order_observations SET as_of_ns=31 WHERE seq=1")
    with pytest.raises(EconomicStoreError, match="contradict"):
        store.inspect_order_observations()


def test_readonly_inspection_cannot_capture_or_create_missing_journal(tmp_path):
    store = journal(tmp_path)
    readonly = EconomicEventStore(store.path, "SIM-001", readonly=True)
    with pytest.raises(EconomicStoreError, match="read-only"):
        readonly.capture_order_observation(
            order_document(), source_namespace="broker", expected_head=readonly.load().evidence_head
        )
    with pytest.raises(EconomicStoreError, match="does not exist"):
        EconomicEventStore(tmp_path / "missing.db", "SIM-001", readonly=True)


def test_database_insert_failure_rolls_back_order_observation(tmp_path):
    store = journal(tmp_path)
    before = store.load()
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER reject_observation BEFORE INSERT ON external_order_observations "
            "BEGIN SELECT RAISE(ABORT, 'simulated commit failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="simulated commit failure"):
        store.capture_order_observation(order_document(), source_namespace="broker", expected_head=before.evidence_head)
    assert store.inspect_order_observations() == ()
    assert store.load() == before


def test_idempotent_retry_cannot_launder_a_corrupt_stored_head(tmp_path):
    store = journal(tmp_path)
    raw = order_document()
    correct_head = store.load().evidence_head
    store.capture_order_observation(raw, source_namespace="broker", expected_head=correct_head)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE external_order_observations SET evidence_head=? WHERE seq=1", ("e" * 64,))
    with pytest.raises(EconomicStoreError, match="corrupted historical ledger head"):
        store.capture_order_observation(raw, source_namespace="broker", expected_head="e" * 64)
    assert store.load().evidence_head == correct_head


def test_idempotent_retry_rejects_historically_rebound_event_count(tmp_path):
    store = journal(tmp_path)
    raw = order_document()
    head = store.load().evidence_head
    store.capture_order_observation(raw, source_namespace="broker", expected_head=head)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE external_order_observations SET event_count=0 WHERE seq=1")
    with pytest.raises(EconomicStoreError, match="corrupted historical ledger head"):
        store.capture_order_observation(raw, source_namespace="broker", expected_head=head)
