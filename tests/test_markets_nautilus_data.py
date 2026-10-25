"""Nautilus 2.0.0rc7 crypto read-only data conversion and time-causality tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.model import AggressorSide, Bar, TradeTick

from lab.markets.domain import AssetClass, ContractKind, Instrument, Side, Trade
from lab.markets.nautilus_data import nautilus_instrument_id, to_nautilus_completed_bar, to_nautilus_trade
from lab.markets.okx_public import MarketDataError, PublicCandle

D = Decimal


def contract() -> Instrument:
    return Instrument(
        "OKX",
        "BTC-USDT-SWAP",
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USDT",
        D("0.1"),
        D("0.001"),
        D("0.001"),
        D("5"),
    )


def test_true_trade_timestamp_and_aggressor_are_preserved() -> None:
    sample = Trade("101", 1_000_000_000, 1_200_000_000, D("100.1"), D("0.025"), Side.SELL, "0")
    converted = to_nautilus_trade(sample, contract())
    assert isinstance(converted, TradeTick)
    assert converted.ts_event == 1_000_000_000
    assert converted.ts_init == 1_200_000_000
    assert converted.aggressor_side == AggressorSide.SELL
    assert str(converted.price) == "100.1"
    assert str(converted.size) == "0.025"
    assert str(converted.instrument_id) == "BTC-USDT-SWAP.OKX"
    assert converted.instrument_id == nautilus_instrument_id(contract())


def test_public_trade_not_forced_to_order_minimum_but_uses_venue_step() -> None:
    sample = Trade("123", 1, 2, D("100.0"), D("0.001"))
    assert isinstance(to_nautilus_trade(sample, contract()), TradeTick)
    with pytest.raises(ValueError, match="step"):
        to_nautilus_trade(Trade("124", 1, 2, D("100.0"), D("0.0005")), contract())
    with pytest.raises(ValueError, match="tick"):
        to_nautilus_trade(Trade("125", 1, 2, D("100.05"), D("0.01")), contract())


def test_completed_1m_bar_is_never_a_trade_and_cannot_arrive_early() -> None:
    bar = PublicCandle(
        "BTC-USDT-SWAP",
        0,
        60_000_000_000,
        D("100.0"),
        D("101.0"),
        D("99.0"),
        D("100.2"),
        D("3.500"),
    )
    with pytest.raises(MarketDataError, match="before it closes"):
        to_nautilus_completed_bar(bar, contract(), received_ns=59_000_000_000)
    converted = to_nautilus_completed_bar(bar, contract(), received_ns=65_000_000_000)
    assert isinstance(converted, Bar)
    assert not isinstance(converted, TradeTick)
    assert converted.ts_event == 60_000_000_000
    assert converted.ts_init == 65_000_000_000
    assert str(converted.close) == "100.2"
    assert str(converted.volume) == "3.500"


def test_explicit_instrument_binding_and_price_grid() -> None:
    bar = PublicCandle(
        "BTC-USDT",
        0,
        60_000_000_000,
        D("100"),
        D("100"),
        D("100"),
        D("100"),
        D("1"),
    )
    with pytest.raises(MarketDataError, match="instrument"):
        to_nautilus_completed_bar(bar, contract(), received_ns=65_000_000_000)
    off_grid = PublicCandle(
        "BTC-USDT-SWAP",
        0,
        60_000_000_000,
        D("100.05"),
        D("100.1"),
        D("100.0"),
        D("100.05"),
        D("1"),
    )
    with pytest.raises(ValueError, match="tick"):
        to_nautilus_completed_bar(off_grid, contract(), received_ns=65_000_000_000)
