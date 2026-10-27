"""Independent broker-neutral statement vs our engine F1.2 parity tests."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from lab.account_observation import (
    parse_external_account_statement,
    reconcile_external_account,
)
from lab.engine_events import (
    CashFact,
    EngineEconomicBook,
    EngineFactError,
    FillFact,
)


def D(text):
    return Decimal(text)


def book():
    return (
        EngineEconomicBook("SIM-001")
        .append(
            CashFact(
                "capital",
                "activity:capital",
                1,
                "CAPITAL_IN",
                D("1000"),
                "BROKER_CASH_ACTIVITY",
                "SETTLED",
                "a" * 64,
            )
        )
        .append(FillFact("execution", "trade:execution", 2, "SPY", "BUY", D("2"), D("100"), D("0.25")))
    )


def statement_bytes(**changes):
    document = {
        "schema_version": 1,
        "account_id": "SIM-001",
        "record_id": "report-1",
        "as_of_ns": 3,
        "source_kind": "BROKER_ACCOUNT_STATEMENT",
        "currency": "USD",
        "cash": "799.75",
        "positions": [{"symbol": "SPY", "quantity": "2"}],
        "complete_instrument_scope": True,
    }
    document.update(changes)
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()


def test_external_account_statement_agrees_with_two_arithmetic_models():
    account = book()
    observation = parse_external_account_statement(
        statement_bytes(), source_namespace="paper:read-only", expected_account_id="SIM-001"
    )
    result = reconcile_external_account(account, observation)
    assert result.matches
    assert result.internal_projection_matches
    assert result.cash_delta == D("0")
    assert result.position_deltas == (("SPY", D("0")),)
    assert result.journal_evidence_head == account.evidence_head
    assert result.statement_source_ref == "paper:read-only:report-1"
    assert len(result.statement_sha256) == 64


def test_reported_discrepancy_is_not_automatically_booked_or_silenced():
    account = book()
    observation = parse_external_account_statement(
        statement_bytes(cash="800.25"), source_namespace="broker", expected_account_id="SIM-001"
    )
    parity = reconcile_external_account(account, observation)
    assert not parity.matches
    assert parity.cash_delta == D("0.50")
    assert len(account.events) == 2
    assert account.ledger.balance("cash:USD") == D("799.75")


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": "FOREIGN"},
        {"currency": "EUR"},
        {"cash": 799.75},
        {"cash": "NaN"},
        {"cash": "1E+9999999"},
        {"cash": "1E-30"},
        {"complete_instrument_scope": False},
        {"complete_instrument_scope": 1},
        {"as_of_ns": 0},
        {"as_of_ns": True},
        {"positions": [{"symbol": "SPY", "quantity": 2.0}]},
        {"positions": [{"symbol": "SPY", "quantity": "Infinity"}]},
        {"positions": [{"symbol": "SPY", "quantity": "2"}, {"symbol": "SPY", "quantity": "2"}]},
        {"source_kind": "ENGINE_ESTIMATE"},
        {"schema_version": True},
        {"schema_version": 2},
    ],
)
def test_external_statement_refuses_ambiguous_or_unqualified_payload(changes):
    with pytest.raises(EngineFactError):
        parse_external_account_statement(
            statement_bytes(**changes), source_namespace="broker", expected_account_id="SIM-001"
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"garbage",
        b"[]",
        b'{"record_id":"x","record_id":"y"}',
        b'{"cash":NaN}',
        b"\xff",
        b"x" * (128 * 1024 + 1),
    ],
)
def test_untrusted_source_bytes_are_bounded_and_fail_closed(raw):
    with pytest.raises(EngineFactError):
        parse_external_account_statement(raw, source_namespace="broker", expected_account_id="SIM-001")


def test_stale_external_observation_and_cross_account_reconciliation_are_refused():
    account = book()
    foreign = parse_external_account_statement(
        statement_bytes(account_id="OTHER"), source_namespace="broker", expected_account_id="OTHER"
    )
    stale = parse_external_account_statement(
        statement_bytes(as_of_ns=1), source_namespace="broker", expected_account_id="SIM-001"
    )
    with pytest.raises(EngineFactError):
        reconcile_external_account(account, foreign)
    with pytest.raises(EngineFactError):
        reconcile_external_account(account, stale)


def test_account_reported_missing_or_extra_instruments_fail_numerical_parity():
    account = book()
    for holdings in ([], [{"symbol": "QQQ", "quantity": "2"}], [{"symbol": "SPY", "quantity": "3"}]):
        state = parse_external_account_statement(
            statement_bytes(positions=holdings), source_namespace="broker", expected_account_id="SIM-001"
        )
        parity = reconcile_external_account(account, state)
        assert not parity.matches
        assert any(delta != 0 for _, delta in parity.position_deltas)


def test_typed_statement_cannot_bypass_numeric_limits():
    account = book()
    observation = parse_external_account_statement(
        statement_bytes(), source_namespace="broker", expected_account_id="SIM-001"
    )
    with pytest.raises(EngineFactError):
        reconcile_external_account(account, replace(observation, cash=D("1E+99999999")))
    with pytest.raises(EngineFactError):
        reconcile_external_account(account, replace(observation, positions=(("SPY", D("1E+99999999")),)))


def test_decimal_context_does_not_change_external_cash_delta():
    from decimal import localcontext

    account = book()
    statement = parse_external_account_statement(
        statement_bytes(cash="799.750000000000001"),
        source_namespace="broker",
        expected_account_id="SIM-001",
    )
    with localcontext() as ctx:
        ctx.prec = 4
        parity = reconcile_external_account(account, statement)
        assert parity.cash_delta == D("0.000000000000001")
        assert not parity.matches
