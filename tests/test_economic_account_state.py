"""F1.2 read-only native AccountState conservation and provenance tests."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lab.economic_account_state import (
    observe_account_state,
    reconcile_account_state,
    validate_account_state_sequence,
)
from lab.economic_ledger import EconomicLedger
from lab.economic_observation import (
    NativeCashObservation,
    NativeFillObservation,
    NativePositionObservation,
    ObservationError,
)


def d(v):
    return Decimal(v)


def money(v, currency="USD"):
    return SimpleNamespace(currency=SimpleNamespace(code=currency), as_decimal=lambda: d(v))


def fake_state(
    *, account_id="SIM-001", event_id="evt-1", ts_event=100, ts_init=101,
    reported=True, total="899.75", free="898.75", locked="1", currency="USD",
    kind="CASH", margins=None,
):
    return SimpleNamespace(
        account_id=account_id,
        event_id=event_id,
        ts_event=ts_event,
        ts_init=ts_init,
        account_type=SimpleNamespace(name=kind),
        base_currency=SimpleNamespace(code=currency),
        is_reported=reported,
        balances=[
            SimpleNamespace(
                total=money(total, currency),
                free=money(free, currency),
                locked=money(locked, currency),
            )
        ],
        margins=[] if margins is None else margins,
    )


def test_cash_account_state_reports_exact_balances_and_original_source_class():
    state = observe_account_state(fake_state())
    assert state.account_id == "SIM-001"
    assert state.event_id == "evt-1"
    assert state.ts_event == 100 and state.ts_init == 101
    assert state.is_reported is True
    assert state.cash == d("899.75")
    assert state.free == d("898.75")
    assert state.locked == d("1")

    calculated = observe_account_state(fake_state(reported=False))
    assert calculated.is_reported is False


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "MARGIN"},
        {"kind": "WALLET"},
        {"currency": "EUR"},
        {"total": "899.76"},
        {"locked": "-1", "free": "900.75"},
        {"ts_event": 0},
        {"ts_init": 99},
        {"ts_event": True},
        {"reported": None},
        {"account_id": ""},
        {"event_id": ""},
        {"margins": ["margin"]},
    ],
)
def test_account_state_rejects_bad_evidence(changes):
    with pytest.raises(ObservationError):
        observe_account_state(fake_state(**changes))


def test_account_state_rejects_foreign_non_usd_balance_and_ambiguous_multiple_currency():
    wrong = fake_state()
    wrong.balances[0].locked = money("1", "EUR")
    with pytest.raises(ObservationError):
        observe_account_state(wrong)
    duplicate = fake_state()
    duplicate.balances.append(duplicate.balances[0])
    with pytest.raises(ObservationError):
        observe_account_state(duplicate)
    no_balance = fake_state()
    no_balance.balances = []
    with pytest.raises(ObservationError):
        observe_account_state(no_balance)


def test_account_event_ties_to_independent_journal_and_matching_native_snapshot():
    ledger = (
        EconomicLedger()
        .post_cash("deposit", "CAPITAL_IN", "1000")
        .post_fill("trade1", "SPY", "BUY", "1", "100", commission="0.25")
    )
    cash = NativeCashObservation("SIM-001", 100, d("899.75"), (NativePositionObservation("SPY", d("1")),))
    fill = NativeFillObservation("trade1", "SPY", "BUY", d("1"), d("100"), d("0.25"))
    state = observe_account_state(fake_state())
    result = reconcile_account_state(ledger, cash, (fill,), state)
    assert result.matches
    assert result.trading.fill_ids_checked == ("trade1",)
    assert result.is_reported
    assert result.state_cash_delta == 0

    unreported = reconcile_account_state(ledger, cash, (fill,), replace(state, is_reported=False))
    assert unreported.matches
    assert not unreported.is_reported


def test_account_state_reconciliation_detects_cash_difference_without_manufactured_postings():
    ledger = EconomicLedger().post_cash("deposit", "CAPITAL_IN", "1000")
    cash = NativeCashObservation("SIM-001", 100, d("1000"), (NativePositionObservation("SPY", d("0")),))
    state = observe_account_state(fake_state(total="999", free="999", locked="0"))
    result = reconcile_account_state(ledger, cash, (), state)
    assert not result.matches
    assert result.trading.matches
    assert result.state_cash_delta == d("1")
    assert len(ledger.journal) == 1


@pytest.mark.parametrize(
    "changes",
    [{"account_id": "SIM-002"}, {"ts_event": 99}, {"ts_event": 101}],
)
def test_account_state_reconciliation_refuses_foreign_or_stale_events(changes):
    ledger = EconomicLedger()
    cash = NativeCashObservation("SIM-001", 100, d("0"), (NativePositionObservation("SPY", d("0")),))
    state = observe_account_state(fake_state(total="0", free="0", locked="0"))
    with pytest.raises(ObservationError):
        reconcile_account_state(ledger, cash, (), replace(state, **changes))


def test_account_state_sequence_requires_one_account_and_monotonic_unique_events():
    first = observe_account_state(fake_state())
    second = observe_account_state(fake_state(event_id="evt-2", ts_event=200, ts_init=201))
    assert validate_account_state_sequence((first, second)) == ("evt-1", "evt-2")
    for sequence in (
        (),
        (first, first),
        (second, first),
        (first, replace(second, event_id="evt-1")),
        (first, replace(second, account_id="OTHER")),
        (first, replace(second, ts_init=1)),
    ):
        with pytest.raises(ObservationError):
            validate_account_state_sequence(sequence)


def test_pinned_nautilus_account_state_and_account_balance_are_observed_without_floats():
    """Use actual Nautilus 2.0.0rc7 model AccountState/AccountBalance classes."""
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model import AccountBalance, AccountId, AccountState, AccountType, Currency, Money

    usd = Currency.from_str("USD")
    total = Money.from_decimal(d("899.75"), usd)
    locked = Money.from_decimal(d("1.00"), usd)
    free = Money.from_decimal(d("898.75"), usd)
    event = AccountState(
        AccountId.from_str("SIM-001"),
        AccountType.CASH,
        [AccountBalance(total, locked, free)],
        [],
        True,
        UUID4(),
        100,
        101,
        usd,
    )
    observation = observe_account_state(event)
    assert observation.cash == d("899.75")
    assert observation.locked == d("1")
    assert observation.is_reported
