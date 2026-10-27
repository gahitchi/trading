"""F1.3 explicitly classified settled cash semantics, no executable trades."""

import json
from decimal import Decimal

import pytest

from lab.account_projection import compare_projection, project_account
from lab.economic_ledger import EconomicLedger
from lab.economic_store import EconomicEventStore
from lab.engine_events import EngineFactError


def D(v):
    return Decimal(v)


def record(kind, amount, seq, *, source="BROKER_CASH_ACTIVITY", status="SETTLED"):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "event_id": f"cash{seq}",
            "record_id": f"source{seq}",
            "timestamp_ns": seq,
            "kind": kind,
            "source_kind": source,
            "settlement_status": status,
            "currency": "USD",
            "amount": amount,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def test_typed_cash_accruals_and_obligations_conserve_exactly_on_restart(tmp_path):
    store = EconomicEventStore(tmp_path / "settlements.sqlite3", "SIM-001")
    scenarios = [
        ("CAPITAL_IN", "1000"),
        ("DIVIDEND", "5"),
        ("DIVIDEND_WITHHOLDING", "1.5"),
        ("INTEREST_CREDIT", "0.25"),
        ("BORROW_FEE", "0.20"),
        ("BORROW_REBATE", "0.04"),
        ("SHORT_DISTRIBUTION", "0.40"),
        ("FINANCING", "0.60"),
        ("CAPITAL_OUT", "10"),
    ]
    for number, (kind, amount) in enumerate(scenarios, 1):
        book = store.append_cash_record(record(kind, amount, number), source_namespace="broker")
    restored = EconomicEventStore(store.path, "SIM-001").load()
    assert restored == book
    assert restored.ledger.balance("cash:USD") == D("992.59")
    assert restored.ledger.balance("expense:withholding") == D("1.5")
    assert restored.ledger.balance("income:interest") == D("-0.25")
    assert restored.ledger.balance("income:borrow_rebate") == D("-0.04")
    assert restored.ledger.balance("expense:short_distribution") == D("0.40")
    assert restored.ledger.balance("equity:capital") == D("-990")
    assert all(item.kind != "FILL" for item in restored.ledger.journal)
    assert compare_projection(restored).matches
    assert project_account(restored).cash == D("992.59")


@pytest.mark.parametrize(
    ("kind", "source", "status"),
    [
        ("INTEREST_CREDIT", "BANK_TRANSFER", "SETTLED"),
        ("BORROW_REBATE", "BANK_TRANSFER", "SETTLED"),
        ("DIVIDEND_WITHHOLDING", "BANK_TRANSFER", "SETTLED"),
        ("SHORT_DISTRIBUTION", "BANK_TRANSFER", "SETTLED"),
        ("SHORT_DISTRIBUTION", "BROKER_CASH_ACTIVITY", "ESTIMATED"),
        ("DIVIDEND_WITHHOLDING", "BROKER_CASH_ACTIVITY", "PENDING"),
    ],
)
def test_unclassified_sources_and_pending_obligations_cannot_be_posted(tmp_path, kind, source, status):
    store = EconomicEventStore(tmp_path / "unsafe.sqlite3", "SIM-001")
    with pytest.raises(EngineFactError):
        store.append_cash_record(record(kind, "1", 1, source=source, status=status), source_namespace="broker")
    assert not store.load().events


def test_signed_cash_movements_are_not_silently_mapped_to_wrong_accounts():
    ledger = EconomicLedger().post_cash("deposit", "CAPITAL_IN", "100")
    ledger = ledger.post_cash("interest", "INTEREST_CREDIT", "0.25")
    ledger = ledger.post_cash("tax", "DIVIDEND_WITHHOLDING", "0.10")
    ledger = ledger.post_cash("manufactured", "SHORT_DISTRIBUTION", "2")
    ledger.audit()
    assert ledger.balance("cash:USD") == D("98.15")
    assert ledger.balance("income:interest") == D("-0.25")
    assert ledger.balance("expense:withholding") == D("0.10")
    assert ledger.balance("expense:short_distribution") == D("2")
