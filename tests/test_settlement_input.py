"""F1.3 cash-statement custody checks: independent of brokers and Nautilus."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from lab.engine_events import EngineEconomicBook, EngineFactError
from lab.settlement_input import append_settled_cash_record, parse_settled_cash_record


def document(**changes):
    data = {
        "schema_version": 1,
        "account_id": "SIM-001",
        "event_id": "deposit-1",
        "record_id": "cash-1",
        "timestamp_ns": 1,
        "kind": "CAPITAL_IN",
        "source_kind": "BROKER_CASH_ACTIVITY",
        "settlement_status": "SETTLED",
        "currency": "USD",
        "amount": "1000.00",
    }
    data.update(changes)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def test_statement_bytes_and_account_identity_feed_exact_independent_journal():
    raw = document()
    book = append_settled_cash_record(EngineEconomicBook("SIM-001"), raw, source_namespace="brokerA")
    book.audit()
    fact = book.events[0]
    assert fact.source_ref == "brokerA:cash-1"
    assert fact.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert fact.amount == Decimal("1000.00")
    assert book.ledger.balance("cash:USD") == Decimal("1000.00")
    assert book.ledger.balance("equity:capital") == Decimal("-1000.00")


def test_classified_dividend_and_carry_are_not_fake_fills():
    book = append_settled_cash_record(EngineEconomicBook("SIM-001"), document(), source_namespace="brokerA")
    dividend = document(event_id="div", record_id="d-1", timestamp_ns=2, kind="DIVIDEND", amount="2.50")
    financing = document(event_id="interest", record_id="f-1", timestamp_ns=3, kind="FINANCING", amount="0.25")
    book = append_settled_cash_record(book, dividend, source_namespace="brokerA")
    book = append_settled_cash_record(book, financing, source_namespace="brokerA")
    book.audit()
    assert book.ledger.balance("cash:USD") == Decimal("1002.25")
    assert all(event.kind != "FILL" for event in book.ledger.journal)


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": "OTHER"},
        {"currency": "EUR"},
        {"amount": 1.0},
        {"amount": "NaN"},
        {"amount": "-1"},
        {"amount": "0"},
        {"timestamp_ns": 0},
        {"timestamp_ns": True},
        {"settlement_status": "EXPECTED"},
        {"settlement_status": "PENDING"},
        {"source_kind": "UNKNOWN"},
        {"kind": "UNKNOWN_EXTERNAL_CASH"},
        {"kind": "FINANCING", "source_kind": "BANK_TRANSFER"},
        {"record_id": "bad record"},
        {"schema_version": True},
    ],
)
def test_reject_invalid_or_unsettled_records(changes):
    original = EngineEconomicBook("SIM-001")
    with pytest.raises(EngineFactError):
        append_settled_cash_record(original, document(**changes), source_namespace="brokerA")
    assert original.events == ()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"[]",
        b"not a JSON record",
        b'{"schema_version":1,"schema_version":1}',
        b'{"amount": NaN}',
        b"\xff",
        b"0" * 70000,
    ],
)
def test_refuse_malformed_or_ambiguous_source_documents(raw):
    with pytest.raises(EngineFactError):
        parse_settled_cash_record(raw, source_namespace="brokerA", expected_account_id="SIM-001")


def test_append_rejects_duplicate_statement_record_and_reordered_observations():
    book = append_settled_cash_record(EngineEconomicBook("SIM-001"), document(), source_namespace="brokerA")
    with pytest.raises(EngineFactError):
        append_settled_cash_record(
            book,
            document(event_id="deposit-2", timestamp_ns=2, amount="9.00"),
            source_namespace="brokerA",
        )
    with pytest.raises(EngineFactError):
        append_settled_cash_record(
            book,
            document(event_id="deposit-3", record_id="cash-3", timestamp_ns=0),
            source_namespace="brokerA",
        )


def test_changing_original_evidence_bytes_invalidates_provenance_digest():
    raw = document()
    book = append_settled_cash_record(EngineEconomicBook("SIM-001"), raw, source_namespace="brokerA")
    changed_raw = raw.replace(b"1000.00", b"1001.00")
    assert raw != changed_raw
    changed = append_settled_cash_record(EngineEconomicBook("SIM-001"), changed_raw, source_namespace="brokerA")
    assert changed.evidence_head != book.evidence_head
    forged = replace(book, events=(replace(book.events[0], source_sha256=changed.events[0].source_sha256),))
    with pytest.raises(EngineFactError):
        forged.audit()
