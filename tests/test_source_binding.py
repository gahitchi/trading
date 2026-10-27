"""Bind USD source record content, not just its unkeyed byte digest.

The namespace comes from the external adapter and is not authenticated by
the record itself. Tests here do not claim settlement finality.
"""

import json
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from lab.economic_store import EconomicEventStore, EconomicStoreError
from lab.engine_events import FillFact, SplitFact
from lab.settlement_input import parse_settled_cash_in_lieu_record, parse_settled_cash_record


def cash_bytes(**updates):
    data = {
        "schema_version": 1,
        "account_id": "SIM-001",
        "event_id": "deposit",
        "record_id": "cash-001",
        "timestamp_ns": 100,
        "kind": "CAPITAL_IN",
        "source_kind": "BROKER_CASH_ACTIVITY",
        "settlement_status": "SETTLED",
        "currency": "USD",
        "amount": "100.00",
    }
    data.update(updates)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def fraction_bytes(**updates):
    data = {
        "schema_version": 1,
        "account_id": "SIM-001",
        "event_id": "fraction",
        "record_id": "fraction-001",
        "timestamp_ns": 102,
        "kind": "CASH_IN_LIEU",
        "source_kind": "BROKER_CASH_ACTIVITY",
        "settlement_status": "SETTLED",
        "currency": "USD",
        "symbol": "SPY",
        "side": "LONG",
        "quantity": "0.5",
        "cash": "60",
    }
    data.update(updates)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": Decimal("100.01")},
        {"timestamp_ns": 101},
        {"kind": "DIVIDEND"},
        {"event_id": "not-the-event"},
        {"source_ref": "broker:other-record"},
        {"settlement_status": "PENDING"},
        {"source_kind": "BANK_TRANSFER"},
    ],
)
def test_cash_record_cannot_be_reinterpreted_as_different_claim(tmp_path, changes):
    raw = cash_bytes()
    original = parse_settled_cash_record(raw, source_namespace="broker", expected_account_id="SIM-001")
    store = EconomicEventStore(tmp_path / "cash.sqlite3", "SIM-001")
    with pytest.raises(EconomicStoreError):
        store.append(replace(original, **changes), source_bytes=raw)
    assert store.load().events == ()


def test_replay_reparses_original_bytes_not_just_matching_digest(tmp_path):
    raw = cash_bytes(record_id="cash:sub-ledger:001")
    original = parse_settled_cash_record(raw, source_namespace="broker:statement", expected_account_id="SIM-001")
    store = EconomicEventStore(tmp_path / "cash.sqlite3", "SIM-001")
    book = store.append(original, source_bytes=raw)
    assert book.ledger.balance("cash:USD") == Decimal("100")
    assert store.load() == book

    # Attack with inconsistent source economics while retaining the actual bytes.
    # Recompute the economic head and journal head, as a privileged DB writer can.
    alternate = replace(original, amount=Decimal("500"))
    forged_book = type(book)(book.account_id).append(alternate)
    from lab.economic_store import _encode

    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE economic_events SET payload=?,evidence_head=?,ledger_head=? WHERE seq=1",
            (_encode(alternate), forged_book.evidence_head, forged_book.ledger.journal[-1].sha256),
        )
    with pytest.raises(EconomicStoreError, match="claimed economic fields"):
        store.load()


@pytest.mark.parametrize(
    "updates",
    [
        {"cash": Decimal("61")},
        {"quantity": Decimal("0.25")},
        {"side": "SHORT"},
        {"timestamp_ns": 103},
        {"symbol": "QQQ"},
        {"event_id": "different"},
        {"source_ref": "broker:another"},
        {"settlement_status": "PENDING"},
    ],
)
def test_cash_in_lieu_original_source_must_match_all_claimed_fields(tmp_path, updates):
    raw = fraction_bytes()
    original = parse_settled_cash_in_lieu_record(raw, source_namespace="broker", expected_account_id="SIM-001")
    store = EconomicEventStore(tmp_path / "fraction.sqlite3", "SIM-001")
    with pytest.raises(EconomicStoreError):
        store.append(replace(original, **updates), source_bytes=raw)
    assert store.load().events == ()


def test_durable_fractional_source_survives_restart(tmp_path):
    store = EconomicEventStore(tmp_path / "fraction.sqlite3", "SIM-001")
    store.append(FillFact("fill", "trade:fill", 100, "SPY", "BUY", Decimal("1"), Decimal("100"), Decimal(0)))
    store.append(SplitFact("split", "action:split", 101, "SPY", 1, 2))
    original = store.append_cash_in_lieu_record(fraction_bytes(), source_namespace="broker")
    assert original.ledger.position("SPY") == 0
    assert store.load() == original
