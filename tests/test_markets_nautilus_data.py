"""Nautilus 2.0.0rc7 crypto read-only data conversion and time-causality tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.model import AggressorSide, Bar, MarkPriceUpdate, TradeTick

from lab.markets.domain import AssetClass, ContractKind, Instrument, MarkPrice, Side, Trade
from lab.markets.nautilus_data import (
    build_causal_tape,
    nautilus_instrument_id,
    to_nautilus_completed_bar,
    to_nautilus_mark,
    to_nautilus_trade,
)
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
    with pytest.raises(MarketDataError, match="signed-64-bit"):
        to_nautilus_completed_bar(bar, contract(), received_ns=2**63)
    with pytest.raises(MarketDataError, match="signed-64-bit"):
        to_nautilus_completed_bar(bar, contract(), received_ns=True)
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


def test_native_mark_price_is_neither_last_trade_nor_quote_and_preserves_receipt() -> None:
    mark = MarkPrice("mark-9", 100_000, 200_000, D("110.5"))
    converted = to_nautilus_mark(mark, contract())
    assert isinstance(converted, MarkPriceUpdate)
    assert not isinstance(converted, TradeTick)
    assert converted.ts_event == 100_000
    assert converted.ts_init == 200_000
    assert str(converted.value) == "110.5"
    assert str(converted.instrument_id) == "BTC-USDT-SWAP.OKX"
    with pytest.raises(ValueError, match="tick"):
        to_nautilus_mark(MarkPrice("mark-bad", 1, 1, D("110.55")), contract())


def test_causal_tape_is_native_ordered_and_non_executable() -> None:
    observations = (
        MarkPrice("mark1", 10, 20, D("100.0")),
        Trade("trade1", 21, 30, D("100.1"), D("0.020"), Side.BUY),
        MarkPrice("mark2", 22, 40, D("100.2")),
    )
    tape = build_causal_tape(contract(), observations)
    assert tape.instrument_id == contract().instrument_id
    assert tape.last_received_ns == 40
    assert tape.execution_qualified is False
    assert [type(e) for e in tape.events] == [MarkPriceUpdate, TradeTick, MarkPriceUpdate]
    assert [e.ts_event for e in tape.events] == [10, 21, 22]
    assert [e.ts_init for e in tape.events] == [20, 30, 40]


def test_causal_tape_rejects_reordering_duplicates_and_unqualified_inputs() -> None:
    a = MarkPrice("m1", 10, 15, D("100.0"))
    b = Trade("t1", 11, 20, D("100.1"), D("0.020"))
    with pytest.raises(ValueError, match="without source"):
        build_causal_tape(contract(), ())
    with pytest.raises(ValueError, match="receipt-time regression"):
        build_causal_tape(contract(), (b, a))
    with pytest.raises(ValueError, match="exchange-time regression"):
        build_causal_tape(contract(), (b, MarkPrice("m2", 9, 25, D("100.2"))))
    with pytest.raises(ValueError, match="duplicate"):
        build_causal_tape(contract(), (a, a))
    with pytest.raises(TypeError, match="source trades"):
        build_causal_tape(contract(), (a, object()))


def test_reconstructed_historical_snapshots_cannot_be_reordered_into_replay() -> None:
    # A snapshot can contain old events with identical receipt timestamps.
    # Feeding them newest-first and silently sorting would introduce
    # hindsight to a strategy. Fail closed instead.
    old = Trade("old", 1, 100, D("100.0"), D("0.010"))
    new = Trade("new", 2, 100, D("100.1"), D("0.010"))
    with pytest.raises(ValueError, match="exchange-time regression"):
        build_causal_tape(contract(), (new, old))
