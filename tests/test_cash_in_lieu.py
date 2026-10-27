"""F1.3 fractional nontrade settlement, no fake market prints."""

import json
import sqlite3
from decimal import Decimal

import pytest

from lab.account_projection import compare_projection, project_account
from lab.economic_ledger import EconomicLedger, EconomicLedgerError
from lab.economic_store import EconomicEventStore
from lab.engine_events import EngineFactError, FillFact, SplitFact


def D(x):
    return Decimal(x)


def receipt(*, side="LONG", quantity="0.5", cash="60", timestamp_ns=3, currency="USD"):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "event_id": "cil-1",
            "record_id": "broker-cil-1",
            "timestamp_ns": timestamp_ns,
            "kind": "CASH_IN_LIEU",
            "source_kind": "BROKER_CASH_ACTIVITY",
            "settlement_status": "SETTLED",
            "currency": currency,
            "symbol": "SPY",
            "side": side,
            "quantity": quantity,
            "cash": cash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize(
    ("opening_side", "cil_side", "expected_cash", "expected_realized"),
    [
        ("BUY", "LONG", D("-40"), D("-10")),
        ("SELL", "SHORT", D("40"), D("10")),
    ],
)
def test_fifo_cash_in_lieu_survives_restart_and_conserves_economics(
    tmp_path, opening_side, cil_side, expected_cash, expected_realized
):
    store = EconomicEventStore(tmp_path / "fractional.sqlite3", "SIM-001")
    store.append(FillFact("fill", "trade:fill", 1, "SPY", opening_side, D("1"), D("100"), D("0")))
    store.append(SplitFact("reverse", "action:reverse", 2, "SPY", 1, 2))
    ending = store.append_cash_in_lieu_record(receipt(side=cil_side), source_namespace="broker")
    ending.audit()
    assert ending.ledger.position("SPY") == D("0")
    assert ending.ledger.balance("inventory:USD:SPY") == D("0")
    assert ending.ledger.balance("cash:USD") == expected_cash
    assert ending.ledger.balance("income:realized") == expected_realized
    assert ending.ledger.journal[-1].kind == "CASH_IN_LIEU"
    assert not ending.ledger.journal[-1].details[0][0] == "price"
    assert [e.kind for e in ending.ledger.journal].count("FILL") == 1
    assert compare_projection(ending).matches
    assert project_account(ending).quantity("SPY") == 0
    assert EconomicEventStore(store.path, "SIM-001").load() == ending
    with sqlite3.connect(store.path) as db:
        saved = db.execute("SELECT source_bytes FROM economic_events WHERE seq=3").fetchone()[0]
        assert saved == receipt(side=cil_side)


@pytest.mark.parametrize(
    ("quantity", "cash", "side"),
    [
        ("1", "60", "LONG"),
        ("0", "60", "LONG"),
        ("0.6", "60", "LONG"),
        ("0.5", "0", "LONG"),
        ("0.5", "60", "OTHER"),
        ("0.5", "60", "SHORT"),
        ("NaN", "60", "LONG"),
        ("0.5", "Infinity", "LONG"),
    ],
)
def test_invalid_cash_in_lieu_refuses_custody_and_preserves_account(tmp_path, quantity, cash, side):
    store = EconomicEventStore(tmp_path / "fractional.sqlite3", "SIM-001")
    store.append(FillFact("fill", "trade:fill", 1, "SPY", "BUY", D("1"), D("100"), D("0")))
    previous = store.append(SplitFact("reverse", "action:reverse", 2, "SPY", 1, 2))
    with pytest.raises((EngineFactError, EconomicLedgerError)):
        store.append_cash_in_lieu_record(receipt(quantity=quantity, cash=cash, side=side), source_namespace="broker")
    assert store.load() == previous


def test_fractional_basis_with_nonterminating_remainder_fails_without_rounding():
    ledger = EconomicLedger().post_fill("fill", "SPY", "BUY", "3", "0.01")
    with pytest.raises(EconomicLedgerError, match="rounding policy"):
        ledger.post_cash_in_lieu("cash", "SPY", "LONG", "0.1", "0.01")
    assert ledger.position("SPY") == D("3")


@pytest.mark.parametrize("currency", ["EUR", "GBP", "USDT"])
def test_cash_in_lieu_refuses_non_usd_statement_sources(tmp_path, currency):
    store = EconomicEventStore(tmp_path / "foreign.sqlite3", "SIM-001")
    with pytest.raises(EngineFactError):
        store.append_cash_in_lieu_record(receipt(currency=currency), source_namespace="broker")
    assert not store.load().events
