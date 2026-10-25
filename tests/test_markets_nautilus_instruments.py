"""Native Nautilus instrument boundary: no new engine and no account authority."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from nautilus_trader.model import CryptoPerpetual

from lab.markets.domain import AssetClass, ContractKind, Instrument, TradingMode
from lab.markets.nautilus_instruments import LinearContractDefinition, to_nautilus_perpetual

D = Decimal


def instrument(symbol="BTC-USDT-SWAP", *, multiplier="0.01", price_step="0.1", size_step="0.001"):
    return Instrument(
        "OKX",
        symbol,
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USDT",
        D(price_step),
        D(size_step),
        D("0.001"),
        D("5"),
        multiplier=D(multiplier),
        initial_margin=D("0.2"),
        maintenance_margin=D("0.1"),
    )


def spec(**changes):
    base = LinearContractDefinition(
        instrument(),
        "BTC",
        "USDT",
        D("1"),
        D("5"),
        100,
        200,
        "offline_fixture",
    )
    return replace(base, **changes)


def test_real_native_crypto_perpetual_fields_not_parallel_placeholders():
    model = to_nautilus_perpetual(spec())
    assert isinstance(model, CryptoPerpetual)
    assert str(model.id) == "BTC-USDT-SWAP.OKX"
    assert str(model.raw_symbol) == "BTC-USDT-SWAP"
    assert str(model.base_currency) == "BTC"
    assert str(model.quote_currency) == "USDT"
    assert str(model.settlement_currency) == "USDT"
    assert model.is_inverse is False
    assert model.price_precision == 1
    assert model.size_precision == 3
    assert str(model.price_increment) == "0.1"
    assert str(model.size_increment) == "0.001"
    assert str(model.min_quantity) == "0.001"
    assert str(model.multiplier) == "0.01"
    assert model.ts_event == 100
    assert model.ts_init == 200
    assert D(str(model.margin_init)) == D("0.2")
    assert D(str(model.margin_maint)) == D("0.1")
    assert D(str(model.maker_fee)) == D("0.0001")
    assert D(str(model.taker_fee)) == D("0.0005")


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"base_currency": "ETH"}, "symbol/base"),
        ({"base_currency": "usdt"}, "base currency"),
        ({"base_currency": "USDT"}, "base currency"),
        ({"settlement_currency": "BTC"}, "settle"),
        ({"metadata_source": "authenticated_demo"}, "not yet qualified"),
        ({"maker_fee_bps": D("NaN")}, "nonfinite"),
        ({"taker_fee_bps": D("2000")}, "bounds"),
        ({"definition_ns": 300}, "time"),
        ({"observed_ns": 50}, "time"),
        ({"definition_ns": True}, "integer"),
        ({"maker_fee_bps": 0.1}, "exact Decimal"),
    ],
)
def test_invalid_contract_metadata_rejected(mutation, error):
    with pytest.raises((TypeError, ValueError), match=error):
        spec(**mutation)


def test_denies_unqualified_contract_kinds_and_precision():
    with pytest.raises(ValueError, match="linear"):
        spec(instrument=replace(instrument(symbol="BTC-USDT"), kind=ContractKind.SPOT))
    with pytest.raises(ValueError, match="precision"):
        spec(instrument=replace(instrument(), tick_size=D("0.00000000001")))
    with pytest.raises(ValueError, match="symbol"):
        spec(instrument=replace(instrument(), symbol="BTC-USDT-QUANTO"))


def test_native_model_does_not_unlock_broker_routing():
    from lab.markets.adapters import open_execution_adapter
    from lab.markets.domain import UnsupportedMode

    model = to_nautilus_perpetual(spec())
    assert isinstance(model, CryptoPerpetual)
    with pytest.raises(UnsupportedMode):
        open_execution_adapter(TradingMode.BROKER_DEMO, instrument())
    with pytest.raises(UnsupportedMode):
        open_execution_adapter(TradingMode.LIVE, instrument())
