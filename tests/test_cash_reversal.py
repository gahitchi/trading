"""F1.3: immutable original source custody and exact compensating cash reversal."""

import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest

from lab.account_projection import compare_projection
from lab.economic_store import EconomicEventStore, EconomicStoreError
from lab.engine_events import EngineFactError, FillFact
from lab.settlement_input import parse_settled_cash_reversal_record


def cash_record(
    *,
    event_id="dividend-original",
    record_id="bank-original",
    when=1,
    kind="DIVIDEND",
    amount="12.50",
):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "event_id": event_id,
            "record_id": record_id,
            "timestamp_ns": when,
            "kind": kind,
            "source_kind": "BROKER_CASH_ACTIVITY",
            "settlement_status": "SETTLED",
            "currency": "USD",
            "amount": amount,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def reversal_record(
    original_raw,
    *,
    original_ns="broker",
    record_id="correction-one",
    event_id="reverse-dividend",
    amount="12.50",
    when=2,
    **changes,
):
    original = json.loads(original_raw)
    correction = {
        "schema_version": 1,
        "account_id": "SIM-001",
        "event_id": event_id,
        "record_id": record_id,
        "timestamp_ns": when,
        "kind": "CASH_REVERSAL",
        "source_kind": "BROKER_CASH_CORRECTION",
        "settlement_status": "SETTLED",
        "currency": "USD",
        "amount": amount,
        "original_kind": original["kind"],
        "target_event_id": original["event_id"],
        "target_source_ref": f"{original_ns}:{original['record_id']}",
        "target_source_sha256": hashlib.sha256(original_raw).hexdigest(),
    }
    correction.update(changes)
    return json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()


def test_cash_correction_is_exact_opposite_entry_and_preserves_original_bytes(tmp_path):
    db_path = tmp_path / "corrections.sqlite"
    store = EconomicEventStore(db_path, "SIM-001")
    original = cash_record()
    first = store.append_cash_record(original, source_namespace="broker")
    head = first.evidence_head
    assert first.ledger.balance("cash:USD") == Decimal("12.50")
    correction = reversal_record(original)
    second = store.append_cash_reversal_record(correction, source_namespace="broker", expected_head=head)
    assert len(second.events) == 2
    assert second.ledger.balance("cash:USD") == 0
    assert second.ledger.balance("income:dividend") == 0
    assert second.ledger.journal[0].kind == "DIVIDEND"
    assert second.ledger.journal[1].kind == "CASH_REVERSAL"
    assert second.ledger.journal[0].postings == tuple(
        type(post)(post.account, -post.amount) for post in second.ledger.journal[1].postings
    )
    assert compare_projection(second).matches
    assert second == EconomicEventStore(db_path, "SIM-001", readonly=True).load()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT source_bytes FROM economic_events WHERE seq=1").fetchone()[0] == original
        assert conn.execute("SELECT source_bytes FROM economic_events WHERE seq=2").fetchone()[0] == correction
    assert store.append_cash_reversal_record(correction, source_namespace="broker", expected_head=head) == second


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        ("CAPITAL_IN", "125.2"),
        ("CAPITAL_OUT", "1.0"),
        ("DIVIDEND", "2.50"),
        ("DIVIDEND_WITHHOLDING", "0.23"),
        ("INTEREST_CREDIT", "1.00"),
        ("BORROW_FEE", "15"),
        ("SHORT_DISTRIBUTION", "4.5"),
        ("FINANCING", "0.15"),
    ],
)
def test_reversal_compensates_both_positive_and_negative_cash_flows(tmp_path, kind, amount):
    store = EconomicEventStore(tmp_path / "currency.sqlite", "SIM-001")
    original = cash_record(kind=kind, amount=amount)
    first = store.append_cash_record(original, source_namespace="broker")
    restored = store.append_cash_reversal_record(reversal_record(original, amount=amount), source_namespace="broker")
    assert restored.ledger.balance("cash:USD") == 0
    assert len(restored.ledger.journal) == 2
    assert compare_projection(restored).matches
    assert first.ledger.balance("cash:USD") != 0


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "13.00"},
        {"target_event_id": "missing"},
        {"target_source_ref": "broker:foreign"},
        {"target_source_sha256": "b" * 64},
        {"original_kind": "CAPITAL_IN"},
        {"settlement_status": "PENDING"},
        {"source_kind": "BROKER_CASH_ACTIVITY"},
        {"currency": "EUR"},
        {"schema_version": True},
        {"account_id": "OTHER"},
    ],
)
def test_adversarial_correction_refusal_is_atomic(tmp_path, changes):
    store = EconomicEventStore(tmp_path / "negative.sqlite", "SIM-001")
    original = cash_record()
    base = store.append_cash_record(original, source_namespace="broker")
    with pytest.raises((EconomicStoreError, EngineFactError)):
        store.append_cash_reversal_record(reversal_record(original, **changes), source_namespace="broker")
    assert store.load() == base


def test_double_reversal_with_distinct_original_source_is_refused(tmp_path):
    store = EconomicEventStore(tmp_path / "twice.sqlite", "SIM-001")
    original = cash_record()
    store.append_cash_record(original, source_namespace="broker")
    store.append_cash_reversal_record(reversal_record(original), source_namespace="broker")
    before = store.load()
    with pytest.raises((EconomicStoreError, EngineFactError), match="reversed"):
        store.append_cash_reversal_record(
            reversal_record(original, record_id="correction-two", event_id="reverse-again", when=3),
            source_namespace="broker",
        )
    assert store.load() == before


def test_reversal_cannot_target_a_trade_or_source_without_original_cash(tmp_path):
    store = EconomicEventStore(tmp_path / "no-trade-reversal.sqlite", "SIM-001")
    trade = FillFact("fill-one", "synthetic:fill-one", 1, "SPY", "BUY", Decimal("1"), Decimal("100"), Decimal("0"))
    store.append(trade)
    source = cash_record(event_id="fill-one", when=2)
    with pytest.raises((EconomicStoreError, EngineFactError), match="prior settled cash"):
        store.append_cash_reversal_record(reversal_record(source, when=3), source_namespace="broker")
    assert len(store.load().events) == 1


def test_reversal_fails_closed_after_source_or_event_payload_tampering(tmp_path):
    store = EconomicEventStore(tmp_path / "tamper.sqlite", "SIM-001")
    original = cash_record()
    store.append_cash_record(original, source_namespace="broker")
    store.append_cash_reversal_record(reversal_record(original), source_namespace="broker")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE economic_events SET source_bytes=? WHERE seq=2", (b"{}",))
    with pytest.raises((EconomicStoreError, EngineFactError)):
        store.load()


def test_reversal_rejects_unbounded_decimal_and_duplicate_json_keys():
    original = cash_record()
    for payload in (reversal_record(original, amount="1E+99999999"), reversal_record(original, amount="NaN")):
        with pytest.raises((EconomicStoreError, EngineFactError)):
            parse_settled_cash_reversal_record(payload, source_namespace="broker", expected_account_id="SIM-001")
    raw = reversal_record(original)
    duplicate = raw.replace(b'"account_id":"SIM-001",', b'"account_id":"SIM-001","account_id":"SIM-001",')
    with pytest.raises(EngineFactError):
        parse_settled_cash_reversal_record(duplicate, source_namespace="broker", expected_account_id="SIM-001")
