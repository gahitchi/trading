"""F1.2 offline native observation contract tests; no broker sessions."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lab.economic_ledger import EconomicLedger
from lab.economic_observation import (
    NativeCashObservation,
    NativeFillObservation,
    NativePositionObservation,
    ObservationError,
    observe_cash_account,
    observe_native_fill,
    reconcile_native,
)


def d(value: str) -> Decimal:
    return Decimal(value)


def native_money(value: str, currency: str = "USD"):
    return SimpleNamespace(currency=SimpleNamespace(code=currency), as_decimal=lambda: d(value))


def native_value(value: str):
    return SimpleNamespace(as_decimal=lambda: d(value))


def fake_instrument(symbol="SPY", currency="USD"):
    iid = SimpleNamespace(symbol=symbol)
    return SimpleNamespace(id=iid, quote_currency=SimpleNamespace(code=currency))


def fake_fill(
    instrument, *, trade_id="trade1", side="BUY", quantity="1", price="100", commission="0.25"
):
    return SimpleNamespace(
        trade_id=trade_id,
        instrument_id=instrument.id,
        order_side=SimpleNamespace(name=side),
        last_qty=native_value(quantity),
        last_px=native_value(price),
        commission=native_money(commission),
    )


def fake_account(*, kind="CASH", currency="USD", cash="899.75"):
    return SimpleNamespace(
        account_type=SimpleNamespace(name=kind),
        base_currency=SimpleNamespace(code=currency),
        id="SIM-001",
        balance_total=lambda: native_money(cash, currency),
    )


class PortfolioReader:
    def __init__(self, positions):
        self.positions = positions
        self.account_ids = []

    def net_position(self, instrument_id, *, account_id):
        self.account_ids.append(account_id)
        return self.positions[str(instrument_id.symbol)]


def test_native_cash_and_fill_observations_match_independent_business_events():
    instrument = fake_instrument()
    event = fake_fill(instrument)
    ledger = (
        EconomicLedger()
        .post_cash("fund", "CAPITAL_IN", "1000")
        .post_fill("trade1", "SPY", "BUY", "1", "100", commission="0.25")
    )
    portfolio = PortfolioReader({"SPY": d("1")})
    snapshot = observe_cash_account(fake_account(), portfolio, {"SPY": instrument.id}, as_of_ns=123)
    observed = observe_native_fill(event, instrument=instrument)
    assert observed == NativeFillObservation("trade1", "SPY", "BUY", d("1"), d("100"), d("0.25"))
    result = reconcile_native(ledger, snapshot, (observed,))
    assert result.matches
    assert result.cash_delta == 0
    assert result.position_deltas == (("SPY", d("0")),)
    assert result.fill_ids_checked == ("trade1",)
    assert portfolio.account_ids == ["SIM-001"]


@pytest.mark.parametrize(
    ("cash", "position", "expected_cash", "expected_position"),
    [
        ("900.00", "1", d("0.25"), d("0")),
        ("899.75", "2", d("0"), d("1")),
        ("899.74", "0.5", d("-0.01"), d("-0.5")),
    ],
)
def test_independent_observations_detect_cash_and_position_divergence(
    cash, position, expected_cash, expected_position
):
    ledger = EconomicLedger().post_cash("fund", "CAPITAL_IN", "1000").post_fill(
        "trade1", "SPY", "BUY", "1", "100", commission="0.25"
    )
    portfolio = PortfolioReader({"SPY": d(position)})
    snapshot = observe_cash_account(fake_account(cash=cash), portfolio, {"SPY": fake_instrument().id}, as_of_ns=123)
    fill = observe_native_fill(fake_fill(fake_instrument()), instrument=fake_instrument())
    result = reconcile_native(ledger, snapshot, (fill,))
    assert result.cash_delta == expected_cash
    assert result.position_deltas == (("SPY", expected_position),)
    assert not result.matches


def test_refuses_margin_or_unproven_currency_base_and_missing_observation_time():
    portfolio = PortfolioReader({"SPY": d("1")})
    scope = {"SPY": fake_instrument().id}
    for account in (fake_account(kind="MARGIN"), fake_account(currency="EUR")):
        with pytest.raises(ObservationError):
            observe_cash_account(account, portfolio, scope, as_of_ns=123)
    with pytest.raises(ObservationError):
        observe_cash_account(fake_account(), portfolio, scope, as_of_ns=True)
    assert not portfolio.account_ids


def test_refuses_unscoped_native_position_reader_no_silent_fallback():
    class WrongReader:
        def net_position(self, instrument_id):
            return d("1")

    with pytest.raises(ObservationError, match="account-scoped"):
        observe_cash_account(
            fake_account(), WrongReader(), {"SPY": fake_instrument().id}, as_of_ns=123
        )


def test_refuses_non_usd_fees_or_non_usd_instruments_and_missing_fee():
    instrument = fake_instrument()
    for event in (
        replace_not_dataclass(fake_fill(instrument), commission=native_money("1", "EUR")),
        replace_not_dataclass(fake_fill(instrument), commission=None),
    ):
        with pytest.raises(ObservationError):
            observe_native_fill(event, instrument=instrument)
    with pytest.raises(ObservationError):
        observe_native_fill(fake_fill(instrument), instrument=fake_instrument(currency="EUR"))


def replace_not_dataclass(obj, **changes):
    return SimpleNamespace(**{**vars(obj), **changes})


def test_refuses_float_conversion_even_when_float_numerically_equals_decimal():
    instrument = fake_instrument()
    event = replace_not_dataclass(fake_fill(instrument), last_qty=SimpleNamespace(as_decimal=lambda: 1.0))
    with pytest.raises(ObservationError, match="Decimal"):
        observe_native_fill(event, instrument=instrument)
    portfolio = PortfolioReader({"SPY": 1.0})
    with pytest.raises(ObservationError, match="Decimal"):
        observe_cash_account(fake_account(), portfolio, {"SPY": instrument.id}, as_of_ns=123)


def test_fill_identity_or_economics_drift_is_blocking_not_a_tolerated_difference():
    ledger = EconomicLedger().post_fill("trade1", "SPY", "SELL", "1", "100")
    snapshot = NativeCashObservation(
        "SIM-001", 123, d("100"), (NativePositionObservation("SPY", d("-1")),)
    )
    for fills in (
        (),
        (NativeFillObservation("other", "SPY", "SELL", d("1"), d("100"), d("0")),),
        (NativeFillObservation("trade1", "SPY", "SELL", d("1"), d("100"), d("0")),) * 2,
        (NativeFillObservation("trade1", "SPY", "SELL", d("1"), d("101"), d("0")),),
    ):
        with pytest.raises(ObservationError):
            reconcile_native(ledger, snapshot, fills)


def test_missing_or_duplicate_native_positions_cannot_be_marked_as_parity():
    ledger = EconomicLedger().post_fill("trade1", "SPY", "BUY", "1", "100")
    fill = NativeFillObservation("trade1", "SPY", "BUY", d("1"), d("100"), d("0"))
    with pytest.raises(ObservationError, match="omit"):
        reconcile_native(ledger, NativeCashObservation("SIM-001", 123, d("-100"), ()), (fill,))
    with pytest.raises(ObservationError, match="duplicate"):
        reconcile_native(
            ledger,
            NativeCashObservation(
                "SIM-001", 123, d("-100"),
                (NativePositionObservation("SPY", d("1")), NativePositionObservation("SPY", d("1"))),
            ),
            (fill,),
        )


def test_invalid_native_ledger_or_precision_cannot_be_accepted():
    ledger = EconomicLedger().post_fill("trade1", "SPY", "BUY", "1", "100")
    invalid = NativeCashObservation("SIM-001", 123, d("-100"), (NativePositionObservation("SPY", 1.0),))
    fill = NativeFillObservation("trade1", "SPY", "BUY", d("1"), d("100"), d("0"))
    with pytest.raises(ObservationError):
        reconcile_native(ledger, invalid, (fill,))
    with pytest.raises(ObservationError):
        reconcile_native(
            ledger,
            NativeCashObservation("SIM-001", 123, d("-100"), (NativePositionObservation("SPY", d("1")),)),
            (replace(fill, price=d("101")),),
        )
