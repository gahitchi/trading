"""Multi-asset spot FX and cash-market risk contracts (no broker integration)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.multiasset import (
    AssetClass,
    FXQuote,
    FXRates,
    Holding,
    Instrument,
    MarketPrice,
    MultiAssetError,
    cash_after_spot_trade,
    portfolio_liquidation_value,
)

T = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
AGE = timedelta(minutes=5)
D = Decimal


def eurusd():
    return Instrument("EUR.USD.SPOT", AssetClass.FX_SPOT, "USD", D("1000"), D("0.00001"), D("1000"), "EUR")


def quotes():
    return FXRates(
        [
            FXQuote("EUR", "USD", D("1.10000"), D("1.10020"), T, "test-feed"),
            FXQuote("USD", "JPY", D("150.00"), D("150.05"), T, "test-feed"),
        ]
    )


def asset(iid="SHARE.XNAS", kind=AssetClass.EQUITY):
    return Instrument(iid, kind, "USD", D("1") if kind != AssetClass.BOND else D("1000"), D("0.01"), D("1") if kind != AssetClass.BOND else D("1000"))


def test_direct_and_inverse_fx_conversions_use_correct_spread_side():
    fx = quotes()
    assert fx.convert(D("100"), "EUR", "USD", as_of=T, max_age=AGE) == D("110.00000")
    assert fx.convert(D("-100"), "EUR", "USD", as_of=T, max_age=AGE) == D("-110.02000")
    assert fx.convert(D("110.02"), "USD", "EUR", as_of=T, max_age=AGE) == D("110.02") / D("1.10020")
    assert fx.convert(D("-110"), "USD", "EUR", as_of=T, max_age=AGE) == D("-100")
    assert fx.convert(D("5"), "USD", "USD", as_of=T, max_age=AGE) == D("5")


def test_triangular_fx_conversion_respects_bid_ask():
    fx = quotes()
    assert fx.convert(D("1"), "EUR", "JPY", as_of=T, max_age=AGE) == D("165.0000000")
    assert fx.convert(D("-1"), "EUR", "JPY", as_of=T, max_age=AGE) == D("-165.0850100")
    with pytest.raises(MultiAssetError, match="no fresh"):
        fx.convert(D("1"), "EUR", "JPY", as_of=T, max_age=AGE, max_hops=1)


def test_stale_future_and_missing_fx_quotes_do_not_invent_rates():
    fx = quotes()
    for when in (T + timedelta(minutes=6), T - timedelta(seconds=1)):
        with pytest.raises(MultiAssetError, match="no fresh"):
            fx.convert(D("10"), "EUR", "USD", as_of=when, max_age=AGE)
    with pytest.raises(MultiAssetError, match="no fresh"):
        fx.convert(D("1"), "EUR", "GBP", as_of=T, max_age=AGE)
    with pytest.raises(MultiAssetError, match="max_age"):
        fx.convert(D("1"), "EUR", "USD", as_of=T, max_age=timedelta(days=8))


def test_invalid_rate_and_duplicated_currency_pairs_fail():
    with pytest.raises(MultiAssetError, match="crossed"):
        FXQuote("EUR", "USD", D("1.2"), D("1.1"), T, "bad")
    with pytest.raises(MultiAssetError, match="duplicate"):
        FXRates([FXQuote("EUR", "USD", D("1"), D("1.1"), T, "x"), FXQuote("USD", "EUR", D("0.9"), D("0.95"), T, "x")])
    with pytest.raises(MultiAssetError):
        FXQuote("EUR", "EUR", D("1"), D("1"), T, "same")


def test_spot_fx_buy_and_sell_create_currency_legs_not_a_security_holding():
    inst = eurusd()
    book = {"EUR": D("2000"), "USD": D("5000")}
    quote = MarketPrice(inst.instrument_id, D("1.10000"), D("1.10020"), T)
    after_buy = cash_after_spot_trade(book, inst, side="BUY", base_quantity=D("1000"), market=quote, commission_quote=D("2"), as_of=T, max_age=AGE)
    assert after_buy == {"EUR": D("3000"), "USD": D("3897.80000")}
    assert book == {"EUR": D("2000"), "USD": D("5000")}
    after_sell = cash_after_spot_trade(after_buy, inst, side="SELL", base_quantity=D("1000"), market=quote, commission_quote=D("2"), as_of=T, max_age=AGE)
    assert after_sell["EUR"] == D("2000")
    assert after_sell["USD"] == D("4995.80000")


def test_fx_cash_only_funding_and_quote_freshness_fail_closed():
    inst = eurusd()
    quote = MarketPrice(inst.instrument_id, D("1.10000"), D("1.10020"), T)
    params = dict(instrument=inst, side="BUY", base_quantity=D("1000"), market=quote, commission_quote=D("0"), as_of=T, max_age=AGE)
    with pytest.raises(MultiAssetError, match="insufficient"):
        cash_after_spot_trade({"USD": D("100")}, **params)
    with pytest.raises(MultiAssetError, match="stale"):
        cash_after_spot_trade({"USD": D("3000")}, **(params | {"as_of": T + timedelta(hours=1)}))
    with pytest.raises(MultiAssetError, match="quantity"):
        cash_after_spot_trade({"USD": D("3000")}, **(params | {"base_quantity": D("999")}))
    with pytest.raises(MultiAssetError, match="finite"):
        cash_after_spot_trade({"USD": D("3000")}, **(params | {"commission_quote": D("NaN")}))


def test_bonds_include_accrued_interest_and_equities_use_bid_for_sale_value():
    bond = asset("BOND-ISIN", AssetClass.BOND)
    equity = asset()
    prices = {
        "BOND-ISIN": MarketPrice("BOND-ISIN", D("101.50"), D("102.00"), T, D("1.25")),
        "SHARE.XNAS": MarketPrice("SHARE.XNAS", D("50"), D("50.5"), T),
    }
    total = portfolio_liquidation_value(
        cash={"USD": D("100")}, holdings=[Holding("BOND-ISIN", D("10000")), Holding("SHARE.XNAS", D("2"))],
        instruments={"BOND-ISIN": bond, "SHARE.XNAS": equity}, prices=prices, fx=FXRates([]),
        reporting_currency="USD", as_of=T, max_age=AGE,
    )
    assert total == D("10475")  # 100 + 10000*(101.5+1.25)/100 + 2*50


def test_cross_currency_equity_cash_and_fx_portfolio_valutation():
    value = portfolio_liquidation_value(
        cash={"EUR": D("100"), "USD": D("110")}, holdings=[Holding("SHARE.XNAS", D("2"))],
        instruments={"SHARE.XNAS": asset()},
        prices={"SHARE.XNAS": MarketPrice("SHARE.XNAS", D("50"), D("50.5"), T)},
        fx=quotes(), reporting_currency="EUR", as_of=T, max_age=AGE,
    )
    assert value == D("100") + (D("210") / D("1.10020"))


def test_missing_pricing_short_positions_duplicate_holding_and_fx_security_rejected():
    stock = asset()
    kwargs = dict(
        cash={}, instruments={"SHARE.XNAS": stock},
        prices={"SHARE.XNAS": MarketPrice("SHARE.XNAS", D("10"), D("11"), T)},
        fx=FXRates([]), reporting_currency="USD", as_of=T, max_age=AGE,
    )
    with pytest.raises(MultiAssetError, match="short securities"):
        portfolio_liquidation_value(holdings=[Holding("SHARE.XNAS", D("-1"))], **kwargs)
    with pytest.raises(MultiAssetError, match="duplicate"):
        portfolio_liquidation_value(holdings=[Holding("SHARE.XNAS", D("1")), Holding("SHARE.XNAS", D("1"))], **kwargs)
    with pytest.raises(MultiAssetError, match="spot FX"):
        portfolio_liquidation_value(
            cash={}, holdings=[Holding("EUR.USD.SPOT", D("1000"))],
            instruments={"EUR.USD.SPOT": eurusd()},
            prices={"EUR.USD.SPOT": MarketPrice("EUR.USD.SPOT", D("1.1000"), D("1.1002"), T)},
            fx=quotes(), reporting_currency="USD", as_of=T, max_age=AGE,
        )


def test_live_broker_not_exposed_by_multiasset_module():
    from lab import multiasset

    assert not any(x in vars(multiasset) for x in ("submit_order", "InteractiveBrokersClient", "Broker"))


def test_cyclic_fx_crosses_rejected_instead_of_cherry_picking_route():
    with pytest.raises(MultiAssetError, match="cyclic FX graph"):
        FXRates(
            [
                FXQuote("EUR", "USD", D("1.10"), D("1.11"), T, "sample"),
                FXQuote("USD", "JPY", D("150"), D("151"), T, "sample"),
                FXQuote("EUR", "JPY", D("170"), D("171"), T, "inconsistent"),
            ]
        )
