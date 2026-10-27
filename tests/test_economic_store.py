"""Adversarial F1.4 durability, replay, exact byte custody and restart tests."""

import json
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from lab.economic_store import EconomicEventStore, EconomicStoreError
from lab.engine_events import EngineFactError, FillFact, SplitFact


def D(value):
    return Decimal(value)


def record(*, event_id="deposit", record_id="r1", ns=1, kind="CAPITAL_IN", amount="1000"):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "event_id": event_id,
            "record_id": record_id,
            "timestamp_ns": ns,
            "kind": kind,
            "source_kind": "BROKER_CASH_ACTIVITY",
            "settlement_status": "SETTLED",
            "currency": "USD",
            "amount": amount,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def fill(event_id="t1", source_ref="broker:trade:t1", time=2, side="BUY", qty="2", price="100"):
    return FillFact(event_id, source_ref, time, "SPY", side, D(qty), D(price), D("0.25"))


def test_durable_restart_recovers_exact_cash_positions_lots_and_two_hash_chains(tmp_path):
    path = tmp_path / "economic.sqlite3"
    store = EconomicEventStore(path, "SIM-001")
    source = record()
    first = store.append_cash_record(source, source_namespace="broker")
    store.append(fill(), expected_head=first.evidence_head)
    third = store.append(SplitFact("split", "action:split", 3, "SPY", 2, 1))
    restored = EconomicEventStore(path, "SIM-001").load()
    assert third == restored
    assert restored.ledger.position("SPY") == D("4")
    assert restored.ledger.balance("cash:USD") == D("799.75")
    assert restored.evidence_head != "0" * 64
    assert len(restored.ledger.journal) == 3

    with sqlite3.connect(path) as db:
        source_saved = db.execute("SELECT source_bytes FROM economic_events WHERE seq=1").fetchone()[0]
        assert source_saved == source


def test_duplicate_retry_after_commit_is_idempotent_even_with_old_head(tmp_path):
    store = EconomicEventStore(tmp_path / "account.sqlite3", "SIM-001")
    previous = store.load().evidence_head
    original = store.append_cash_record(record(), source_namespace="bank", expected_head=previous)
    retry = store.append_cash_record(record(), source_namespace="bank", expected_head=previous)
    assert original == retry
    assert len(retry.events) == 1
    with pytest.raises(EconomicStoreError, match="conflicting"):
        store.append_cash_record(record(amount="1001"), source_namespace="bank")
    assert store.load() == original


def test_stale_writer_cannot_advance_account_and_failure_preserves_bytes(tmp_path):
    store = EconomicEventStore(tmp_path / "fence.sqlite3", "SIM-001")
    stale = store.load().evidence_head
    funded = store.append_cash_record(record(), source_namespace="broker", expected_head=stale)
    with pytest.raises(EconomicStoreError, match="stale"):
        store.append(fill(), expected_head=stale)
    assert store.load() == funded
    assert store.append(fill(), expected_head=funded.evidence_head).ledger.position("SPY") == D("2")


def test_cross_account_reopen_or_conflicting_source_ref_refused(tmp_path):
    path = tmp_path / "one-account.sqlite3"
    store = EconomicEventStore(path, "SIM-001")
    book = store.append_cash_record(record(), source_namespace="broker")
    with pytest.raises(EconomicStoreError):
        EconomicEventStore(path, "OTHER-002")
    with pytest.raises((EconomicStoreError, EngineFactError, sqlite3.IntegrityError)):
        store.append_cash_record(record(event_id="another", ns=2, amount="2"), source_namespace="broker")
    assert store.load() == book


@pytest.mark.parametrize("column", ["source_bytes", "payload", "evidence_head", "ledger_head", "source_ref"])
def test_database_corruption_fails_closed_and_does_not_rewrite_evidence(tmp_path, column):
    path = tmp_path / "tamper.sqlite3"
    store = EconomicEventStore(path, "SIM-001")
    store.append_cash_record(record(), source_namespace="broker")
    with sqlite3.connect(path) as db:
        db.execute(
            f"UPDATE economic_events SET {column}=? WHERE seq=1",
            (b"other" if column in {"source_bytes", "payload"} else "altered",),
        )
    with pytest.raises((EconomicStoreError, EngineFactError)):
        store.load()


def test_missing_and_reordered_db_history_rejected(tmp_path):
    path = tmp_path / "missing.sqlite3"
    store = EconomicEventStore(path, "SIM-001")
    store.append_cash_record(record(), source_namespace="broker")
    store.append(fill())
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM economic_events WHERE seq=1")
    with pytest.raises(EconomicStoreError, match="sequencing"):
        store.load()


def test_new_append_validations_are_atomic_on_bad_inputs(tmp_path):
    store = EconomicEventStore(tmp_path / "atomic.sqlite3", "SIM-001")
    good = store.append_cash_record(record(), source_namespace="broker")
    with pytest.raises(EconomicStoreError, match="source digest"):
        tampered = replace(good.events[0], event_id="deposit2", source_ref="broker:r2", timestamp_ns=2)
        store.append(tampered, source_bytes=record(amount="9999"))
    assert store.load() == good
    with pytest.raises(EconomicStoreError, match="original source bytes"):
        store.append(good.events[0])
    with pytest.raises(EconomicStoreError, match="noncash source bytes"):
        store.append(fill(), source_bytes=b"unreviewed fill proof")


def test_decimal_inputs_reject_float_and_invalid_event_types_at_storage_boundary(tmp_path):
    store = EconomicEventStore(tmp_path / "typed.sqlite3", "SIM-001")
    with pytest.raises((EconomicStoreError, EngineFactError)):
        store.append(replace(fill(), price=100.0))
    with pytest.raises((EconomicStoreError, EngineFactError)):
        store.append("not a typed fact")
    assert store.load().events == ()


def test_sqlite_pragmas_are_actual_durable_transaction_configuration(tmp_path):
    store = EconomicEventStore(tmp_path / "settings.sqlite3", "SIM-001")
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        # Value is per-connection; _connect() must set FULL for writes.
    with store._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.parametrize(
    "price",
    [
        Decimal("1E+999999999"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1E-50"),
        Decimal("123456789012345678901234567890"),
    ],
)
def test_store_refuses_unbounded_numeric_payload_before_serialization(tmp_path, price):
    store = EconomicEventStore(tmp_path / "bounded.sqlite3", "SIM-001")
    with pytest.raises(EconomicStoreError):
        store.append(replace(fill(), price=price))
    assert store.load().events == ()


def test_rolled_back_writer_does_not_leave_half_persisted_economic_event(tmp_path):
    store = EconomicEventStore(tmp_path / "rollback.sqlite3", "SIM-001")
    initial = store.append_cash_record(record(), source_namespace="broker")
    writer = sqlite3.connect(store.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO economic_events(seq,event_id,source_ref,payload,evidence_head,ledger_head) "
            "VALUES(?,?,?,?,?,?)",
            (2, "orphan", "orphan:fill", b"{}", "f" * 64, "f" * 64),
        )
    finally:
        # Simulate a process failure before COMMIT.
        writer.close()
    assert store.load() == initial
    assert store.append(fill()).ledger.position("SPY") == D("2")


def test_concurrent_writers_with_same_head_cannot_both_commit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = EconomicEventStore(tmp_path / "concurrent.sqlite3", "SIM-001")
    funded = store.append_cash_record(record(), source_namespace="broker")
    original_head = funded.evidence_head
    events = [
        fill(event_id="t-a", source_ref="broker:trade:t-a", time=2),
        fill(event_id="t-b", source_ref="broker:trade:t-b", time=2),
    ]

    def compete(event):
        try:
            store.append(event, expected_head=original_head)
            return "committed"
        except EconomicStoreError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, events))
    assert sorted(results) == ["committed", "refused"]
    assert len(store.load().events) == 2
