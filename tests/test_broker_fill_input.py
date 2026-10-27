"""Adversarial original broker fill custody and exact non-simulated economics."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from lab.account_projection import compare_projection, project_account
from lab.economic_store import EconomicEventStore, EconomicStoreError, _encode
from lab.engine_events import BrokerFillFact, EngineFactError
from lab.fill_input import parse_broker_fill_record


def D(value):
    return Decimal(value)


def receipt(
    *,
    record_id="execution-1",
    event_id="exec-1",
    broker_order_id="order-123",
    timestamp_ns=10,
    side="BUY",
    quantity="2",
    price="100",
    commission="0.25",
    status="EXECUTED",
    source_kind="BROKER_EXECUTION_REPORT",
    quote_currency="USD",
    commission_currency="USD",
):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": record_id,
            "event_id": event_id,
            "timestamp_ns": timestamp_ns,
            "kind": "BROKER_FILL",
            "status": status,
            "source_kind": source_kind,
            "broker_order_id": broker_order_id,
            "symbol": "SPY",
            "side": side,
            "quote_currency": quote_currency,
            "commission_currency": commission_currency,
            "quantity": quantity,
            "price": price,
            "commission": commission,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_original_broker_execution_bytes_are_durable_and_replay_exactly(tmp_path):
    path = tmp_path / "fills.db"
    store = EconomicEventStore(path, "SIM-001")
    raw = receipt()
    book = store.append_broker_fill_record(raw, source_namespace="paper:broker")
    assert type(book.events[0]) is BrokerFillFact
    assert book.events[0].source_sha256 == hashlib.sha256(raw).hexdigest()
    assert book.events[0].source_ref == "paper:broker:execution-1"
    assert book.events[0].broker_order_id == "order-123"
    assert book.ledger.balance("cash:USD") == D("-200.25")
    assert book.ledger.position("SPY") == D("2")
    assert compare_projection(book).matches
    assert project_account(book).cash == D("-200.25")
    assert EconomicEventStore(path, "SIM-001", readonly=True).load() == book
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT source_bytes FROM economic_events").fetchone()[0] == raw


def test_two_partial_executions_for_same_broker_order_are_distinct_events(tmp_path):
    store = EconomicEventStore(tmp_path / "partials.db", "SIM-001")
    first = store.append_broker_fill_record(
        receipt(record_id="partial-1", event_id="exec-1", quantity="0.25", commission="0.01"),
        source_namespace="paper",
    )
    second = store.append_broker_fill_record(
        receipt(record_id="partial-2", event_id="exec-2", timestamp_ns=11, quantity="0.75", commission="0.02"),
        source_namespace="paper",
        expected_head=first.evidence_head,
    )
    assert second.ledger.position("SPY") == D("1")
    assert second.ledger.balance("cash:USD") == D("-100.03")
    assert compare_projection(second).matches
    assert store.load() == second


@pytest.mark.parametrize(
    "changes",
    [
        {"price": "101"},
        {"quantity": "0.1"},
        {"commission": "1"},
        {"side": "SELL"},
        {"timestamp_ns": 999},
        {"broker_order_id": "order-999"},
        {"source_ref": "broker:wrong"},
    ],
)
def test_original_bytes_cannot_be_reused_to_fabricate_other_trade_facts(tmp_path, changes):
    raw = receipt()
    fact = parse_broker_fill_record(raw, source_namespace="paper", expected_account_id="SIM-001")
    store = EconomicEventStore(tmp_path / "forged.db", "SIM-001")
    with pytest.raises(EconomicStoreError):
        store.append(replace(fact, **changes), source_bytes=raw)
    assert store.load().events == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "PENDING"},
        {"status": "PARTIALLY_FILLED"},
        {"status": "CANCELLED"},
        {"source_kind": "SIMULATED"},
        {"quote_currency": "EUR"},
        {"commission_currency": "EUR"},
        {"quantity": 1.0},
        {"quantity": "NaN"},
        {"quantity": "0"},
        {"price": "Infinity"},
        {"price": "0"},
        {"commission": "-1"},
        {"commission": "1E+9999999"},
        {"timestamp_ns": True},
        {"timestamp_ns": 0},
        {"side": "HOLD"},
    ],
)
def test_unqualified_execution_records_are_rejected(tmp_path, kwargs):
    source = receipt(**kwargs)
    store = EconomicEventStore(tmp_path / "unqualified.db", "SIM-001")
    with pytest.raises(EngineFactError):
        store.append_broker_fill_record(source, source_namespace="paper")
    assert not store.load().events


def test_duplicate_fill_retry_idempotent_and_revised_receipt_refused(tmp_path):
    store = EconomicEventStore(tmp_path / "idempotent.db", "SIM-001")
    raw = receipt()
    original = store.append_broker_fill_record(raw, source_namespace="paper")
    assert store.append_broker_fill_record(raw, source_namespace="paper") == original
    with pytest.raises(EconomicStoreError):
        store.append_broker_fill_record(receipt(price="101"), source_namespace="paper")
    assert store.load() == original


def test_replay_rejects_rehashed_forged_trade_with_original_source_bytes(tmp_path):
    store = EconomicEventStore(tmp_path / "forged.db", "SIM-001")
    raw = receipt()
    book = store.append_broker_fill_record(raw, source_namespace="paper")
    altered = replace(book.events[0], price=D("100.01"))
    recreated = type(book)(book.account_id).append(altered)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE economic_events SET payload=?,evidence_head=?,ledger_head=? WHERE seq=1",
            (_encode(altered), recreated.evidence_head, recreated.ledger.journal[-1].sha256),
        )
    with pytest.raises(EconomicStoreError, match="claimed economic fields"):
        store.load()


def test_missing_original_fill_source_bytes_never_means_verified_broker_fill(tmp_path):
    raw = receipt()
    fact = parse_broker_fill_record(raw, source_namespace="paper", expected_account_id="SIM-001")
    store = EconomicEventStore(tmp_path / "missing.db", "SIM-001")
    with pytest.raises(EconomicStoreError, match="original source bytes"):
        store.append(fact)
    assert store.load().events == ()
