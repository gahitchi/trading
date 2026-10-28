"""F1.3.c cash-account supported-scope admission and historical prefix safety."""

import json
from decimal import Decimal

import pytest

from lab.cash_scope import parse_cash_account_policy, validate_cash_account_scope
from lab.engine_events import EngineEconomicBook, EngineFactError, FillFact, SplitFact


def policy_raw(account="SIM-001", klass="EQUITY", currency="USD", account_type="USD_UNLEVERAGED_CASH"):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": account,
            "record_id": "cash-reference-policy",
            "account_type": account_type,
            "currency": currency,
            "instruments": [
                {"symbol": "SPY", "asset_class": klass, "quote_currency": "USD", "venue_class": "US_LISTED"}
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def policy(raw=None):
    return parse_cash_account_policy(policy_raw() if raw is None else raw, expected_account_id="SIM-001")


def seeded():
    from lab.settlement_input import parse_settled_cash_record

    state = EngineEconomicBook("SIM-001")
    raw = json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "event_id": "deposit",
            "record_id": "bank-01",
            "timestamp_ns": 1,
            "kind": "CAPITAL_IN",
            "source_kind": "BROKER_CASH_ACTIVITY",
            "settlement_status": "SETTLED",
            "currency": "USD",
            "amount": "500",
        },
        sort_keys=True,
        separators=(",", ":")
    ).encode()
    return state.append(parse_settled_cash_record(raw, source_namespace="bank", expected_account_id="SIM-001"))


def fill(state, id_, when, side, qty, price="100"):
    return state.append(
        FillFact(
            id_,
            f"simulated:{id_}",
            when,
            "SPY",
            side,
            Decimal(qty),
            Decimal(price),
            Decimal("1"),
        )
    )


def test_fully_funded_long_buy_sale_and_reverse_split_are_accepted():
    state = seeded()
    state = fill(state, "buy", 2, "BUY", "2")
    state = state.append(SplitFact("reverse-split", "corp:1", 3, "SPY", 1, 2))
    state = fill(state, "sell", 4, "SELL", "1", price="130")
    result = validate_cash_account_scope(state, policy())
    assert result.events_checked == 4
    assert result.supported_instruments_checked == 1
    assert result.final_settled_cash_usd == Decimal("428")
    assert result.min_settled_cash_usd >= 0
    assert not result.broker_account_type_authenticated


def test_closed_intraday_short_history_stays_refused_even_if_final_position_flat():
    state = seeded()
    state = fill(state, "short", 2, "SELL", "1")
    state = fill(state, "cover", 3, "BUY", "1")
    assert state.ledger.position("SPY") == 0
    with pytest.raises(EngineFactError, match="short"):
        validate_cash_account_scope(state, policy())


def test_cash_overdraft_in_any_historical_prefix_is_refused_after_later_deposit():
    state = EngineEconomicBook("SIM-001")
    state = fill(state, "unfunded-buy", 1, "BUY", "1")
    state = seeded_from(state, amount="500", when=2)
    assert state.ledger.balance("cash:USD") > 0
    with pytest.raises(EngineFactError, match="negative"):
        validate_cash_account_scope(state, policy())


def seeded_from(state, *, amount, when):
    from lab.settlement_input import parse_settled_cash_record

    raw = json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "event_id": f"deposit-{when}",
            "record_id": f"bank-{when}",
            "timestamp_ns": when,
            "kind": "CAPITAL_IN",
            "source_kind": "BROKER_CASH_ACTIVITY",
            "settlement_status": "SETTLED",
            "currency": "USD",
            "amount": amount,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return state.append(parse_settled_cash_record(raw, source_namespace="bank", expected_account_id="SIM-001"))


@pytest.mark.parametrize(
    "raw",
    [
        policy_raw(account="OTHER"),
        policy_raw(klass="FOREX"),
        policy_raw(currency="EUR"),
        policy_raw(account_type="MARGIN"),
    ],
)
def test_policy_rejects_unsupported_broker_semantics(raw):
    with pytest.raises(EngineFactError):
        policy(raw)


def test_unfunded_buy_never_autofunds_and_does_not_modify_ledger():
    state = fill(EngineEconomicBook("SIM-001"), "buy", 1, "BUY", "1")
    before = state
    with pytest.raises(EngineFactError):
        validate_cash_account_scope(state, policy())
    assert state == before



def test_local_cash_scope_manifest_cannot_be_mutated_after_source_hash():
    from dataclasses import replace

    baseline = policy()
    altered = replace(baseline, instruments=())
    with pytest.raises(EngineFactError, match="mutated"):
        validate_cash_account_scope(seeded(), altered)
