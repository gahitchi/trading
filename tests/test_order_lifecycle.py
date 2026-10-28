"""F1.4 order lifecycle custody: no broker, network, risk or routing authority."""

import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest

from lab.economic_store import EconomicEventStore, EconomicStoreError
from lab.engine_events import EngineFactError
from lab.order_lifecycle import OrderLifecycleStore, parse_broker_order_event


def D(value):
    return Decimal(value)


def fill_raw(record="fill-1", *, amount="0.4", when=11, order_id="order-1", price="100"):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": record,
            "event_id": f"exec-{record}",
            "timestamp_ns": when,
            "kind": "BROKER_FILL",
            "status": "EXECUTED",
            "source_kind": "BROKER_EXECUTION_REPORT",
            "broker_order_id": order_id,
            "symbol": "SPY",
            "side": "BUY",
            "quote_currency": "USD",
            "commission_currency": "USD",
            "quantity": amount,
            "price": price,
            "commission": "0.01",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def order_raw(
    event_id="ack-1",
    *,
    kind="ACCEPTED",
    when=10,
    cumulative="0",
    remaining="1",
    requested="1",
    order_id="order-1",
    fill=None,
):
    record_id = json.loads(fill)["record_id"] if fill is not None else None
    digest = hashlib.sha256(fill).hexdigest() if fill is not None else None
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": f"order-event-{event_id}",
            "event_id": event_id,
            "timestamp_ns": when,
            "source_kind": "BROKER_ORDER_EVENT",
            "broker_order_id": order_id,
            "kind": kind,
            "symbol": "SPY",
            "side": "BUY",
            "requested_quantity": requested,
            "cumulative_executed_quantity": cumulative,
            "remaining_quantity": remaining,
            "fill_record_id": record_id,
            "fill_source_sha256": digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def make(tmp_path):
    store = EconomicEventStore(tmp_path / "economic.sqlite", "SIM-001")
    return store, OrderLifecycleStore(store)


def append(store, lifecycle, raw, fill=None, head=None):
    return lifecycle.append(
        raw,
        source_namespace="broker",
        expected_head=store.load().evidence_head if head is None else head,
        raw_fill=fill,
    )


def test_two_partial_fills_and_cancel_race_commit_only_real_broker_receipts(tmp_path):
    store, lifecycle = make(tmp_path)
    initial = store.load().evidence_head
    ack = order_raw()
    first = append(store, lifecycle, ack)
    assert first.sequence == 1
    assert first.economic_head == initial
    assert first.unresolved_order_ids == ("order-1",)
    f1 = fill_raw()
    append(store, lifecycle, order_raw("fill-step-1", kind="FILL", when=11, cumulative="0.4", remaining="0.6", fill=f1), f1)
    append(store, lifecycle, order_raw("cancel-request", kind="CANCEL_REQUESTED", when=12, cumulative="0.4", remaining="0.6"))
    f2 = fill_raw("fill-2", amount="0.3", when=13, price="101")
    append(store, lifecycle, order_raw("fill-step-2", kind="FILL", when=13, cumulative="0.7", remaining="0.3", fill=f2), f2)
    result = append(store, lifecycle, order_raw("cancel-ack", kind="CANCELED", when=14, cumulative="0.7", remaining="0"))
    assert result.sequence == 5
    assert result.states[0].status == "CANCELED"
    assert result.states[0].filled_quantity == D("0.7")
    assert not result.unresolved_order_ids
    book = store.load()
    assert len(book.events) == 2
    assert book.ledger.position("SPY") == D("0.7")
    assert lifecycle.inspect() == result
    assert append(store, lifecycle, ack, head=initial) == result
    assert store.load() == book


def test_fill_then_full_execution_is_terminal_and_never_invents_cancel(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    original = fill_raw(amount="1")
    filled = append(
        store,
        lifecycle,
        order_raw("full-fill", kind="FILL", when=11, cumulative="1", remaining="0", fill=original),
        original,
    )
    assert filled.states[0].status == "FILLED"
    before = store.load()
    with pytest.raises(EconomicStoreError, match="terminal"):
        append(store, lifecycle, order_raw("late-cancel", kind="CANCELED", when=12, cumulative="1", remaining="0"))
    assert store.load() == before
    assert lifecycle.inspect() == filled


def test_unexpected_late_fill_after_cancel_confirmation_is_refused(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    append(store, lifecycle, order_raw("cancel", kind="CANCELED", when=12, cumulative="0", remaining="0"))
    late = fill_raw("fill-late", amount="0.3", when=13)
    previous = store.load()
    with pytest.raises(EconomicStoreError, match="terminal"):
        append(
            store,
            lifecycle,
            order_raw("late", kind="FILL", when=13, cumulative="0.3", remaining="0.7", fill=late),
            late,
        )
    assert store.load() == previous


def test_atomic_commit_rolls_back_financial_fill_if_order_event_write_fails(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    before = store.load()
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER force_order_event_failure BEFORE INSERT ON broker_order_events "
            "WHEN NEW.seq=2 BEGIN SELECT RAISE(ABORT, 'forced order insert failure'); END"
        )
    f = fill_raw()
    with pytest.raises(sqlite3.DatabaseError, match="forced order insert failure"):
        append(
            store,
            lifecycle,
            order_raw("fill-rollback", kind="FILL", when=11, cumulative="0.4", remaining="0.6", fill=f),
            f,
        )
    assert store.load() == before
    assert lifecycle.inspect().sequence == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM economic_events").fetchone()[0] == 0


def test_direct_financial_fill_cannot_bypass_tracked_order_transition(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    with pytest.raises(EconomicStoreError, match="atomic order lifecycle"):
        store.append_broker_fill_record(fill_raw(), source_namespace="broker")
    assert store.load().events == ()
    assert lifecycle.inspect().states[0].filled_quantity == 0


def test_restart_only_inspects_unresolved_broker_order_and_never_resubmits(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    readonly = EconomicEventStore(store.path, "SIM-001", readonly=True)
    reopened = OrderLifecycleStore(readonly)
    old = reopened.inspect()
    assert old.unresolved_order_ids == ("order-1",)
    assert old.states[0].status == "ACCEPTED"
    with pytest.raises(EconomicStoreError, match="read-only"):
        reopened.append(
            order_raw("cancel-req", kind="CANCEL_REQUESTED"),
            source_namespace="broker",
            expected_head=readonly.load().evidence_head,
        )
    assert reopened.inspect() == old


def test_duplicate_source_event_refused_and_exact_retry_is_idempotent(tmp_path):
    store, lifecycle = make(tmp_path)
    ack = order_raw()
    head = store.load().evidence_head
    append(store, lifecycle, ack)
    assert append(store, lifecycle, ack, head=head).sequence == 1
    with pytest.raises(EconomicStoreError, match="reused order event identity"):
        append(
            store,
            lifecycle,
            order_raw("ack-1", kind="REJECTED", remaining="0"),
            head=head,
        )
    assert lifecycle.inspect().sequence == 1


def test_duplicate_broker_fill_receipt_on_new_order_event_refused(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    f = fill_raw()
    append(
        store, lifecycle,
        order_raw("fill-one", kind="FILL", when=11, cumulative="0.4", remaining="0.6", fill=f),
        f,
    )
    before = store.load()
    with pytest.raises(EconomicStoreError, match="diverged|duplicated|source"):
        append(
            store, lifecycle,
            order_raw("fill-two", kind="FILL", when=11, cumulative="0.8", remaining="0.2", fill=f),
            f,
        )
    assert store.load() == before


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "OTHER"},
        {"schema_version": True},
        {"kind": "SUBMIT_ORDER"},
        {"requested_quantity": "NaN"},
        {"requested_quantity": "1E+999999"},
        {"cumulative_executed_quantity": "1.01"},
        {"fill_source_sha256": "a" * 64},
    ],
)
def test_parser_refuses_untrusted_order_evidence(change):
    record = json.loads(order_raw())
    record.update(change)
    with pytest.raises((EconomicStoreError, EngineFactError, ValueError)):
        parse_broker_order_event(
            json.dumps(record).encode(), source_namespace="broker", expected_account_id="SIM-001"
        )


def test_parser_rejects_duplicate_keys_and_unbounded_original_bytes():
    raw = order_raw()
    with pytest.raises(EconomicStoreError):
        parse_broker_order_event(
            b'{"schema_version":1,"schema_version":1}',
            source_namespace="broker",
            expected_account_id="SIM-001",
        )
    with pytest.raises(EconomicStoreError, match="bounded"):
        parse_broker_order_event(
            raw + b" " * (64 * 1024),
            source_namespace="broker",
            expected_account_id="SIM-001",
        )


def test_order_source_chain_and_economic_head_tampering_is_detected(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE broker_order_events SET order_chain=? WHERE seq=1", ("f" * 64,))
    with pytest.raises(EconomicStoreError, match="chain"):
        lifecycle.inspect()


def test_reordered_cumulative_fill_and_timestamp_are_rejected(tmp_path):
    store, lifecycle = make(tmp_path)
    append(store, lifecycle, order_raw())
    original = fill_raw()
    append(
        store, lifecycle,
        order_raw("fill-one", kind="FILL", when=11, cumulative="0.4", remaining="0.6", fill=original),
        original,
    )
    new_fill = fill_raw("fill-2", amount="0.3", when=9)
    with pytest.raises(EconomicStoreError, match="terminal|reordered|nonconserving"):
        append(
            store, lifecycle,
            order_raw("fill-reordered", kind="FILL", when=9, cumulative="0.7", remaining="0.3", fill=new_fill),
            new_fill,
        )
    assert len(store.load().events) == 1


def test_preexisting_orphan_receipt_blocks_transition_and_does_not_forge_lifecycle(tmp_path):
    store, lifecycle = make(tmp_path)
    store.append_broker_fill_record(fill_raw(), source_namespace="broker")
    with pytest.raises(EconomicStoreError, match="unlinked execution"):
        append(store, lifecycle, order_raw())
    assert lifecycle.inspect().sequence == 0
    assert len(store.load().events) == 1
