"""Typed read-only crypto data conversion into the *existing* Nautilus event model.

This is a market-data boundary, not a new strategy/risk/execution actor and
never constructs a broker order. A completed OHLCV candle is a Bar ONLY; an
actual observed public trade is a TradeTick. Neither is sufficient to infer
best bid/ask, queue fills, regional account capabilities or collateral.
"""

from __future__ import annotations

from nautilus_trader.model import (
    AggressorSide,
    Bar,
    BarType,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)

from lab.markets.domain import AssetClass, ContractKind, Instrument, Side, Trade
from lab.markets.okx_public import MarketDataError, PublicCandle


def nautilus_instrument_id(instrument: Instrument) -> InstrumentId:
    if instrument.asset_class != AssetClass.CRYPTO or instrument.kind != ContractKind.LINEAR_DERIVATIVE:
        raise ValueError("crypto data bridge currently supports linear contracts only")
    return InstrumentId.from_str(f"{instrument.symbol}.{instrument.venue}")


def _precision(value) -> int:
    return max(0, -value.as_tuple().exponent)


def to_nautilus_trade(trade: Trade, instrument: Instrument) -> TradeTick:
    """An observed trade *only*. No inference of a bid/ask or guaranteed fill."""
    instrument.check_price(trade.price)
    if trade.quantity % instrument.quantity_step:
        raise ValueError("observed trade violates venue quantity step")
    if trade.taker_side == Side.BUY:
        aggressor = AggressorSide.BUYER
    elif trade.taker_side == Side.SELL:
        aggressor = AggressorSide.SELLER
    else:
        aggressor = AggressorSide.NO_AGGRESSOR
    return TradeTick(
        nautilus_instrument_id(instrument),
        Price.from_str(f"{trade.price:.{_precision(instrument.tick_size)}f}"),
        Quantity.from_str(f"{trade.quantity:.{_precision(instrument.quantity_step)}f}"),
        aggressor,
        TradeId(trade.trade_id),
        trade.exchange_ns,
        trade.received_ns,
    )


def to_nautilus_completed_bar(candle: PublicCandle, instrument: Instrument, *, received_ns: int) -> Bar:
    """Preserve close-time ts_event but delay ts_init until the *actual* observation.

    Passing a bar with ts_init==close when it was fetched much later creates
    lookahead leakage. A data-capture manifest must supply the receipt time.
    """
    if candle.instrument_id != instrument.symbol:
        raise MarketDataError("candle instrument does not match normalized contract")
    if candle.source != "OKX_EEA_PUBLIC_CANDLES":
        raise MarketDataError("unqualified public candle origin")
    if type(received_ns) is not int or received_ns < candle.end_ns:
        raise MarketDataError("candle cannot be available before it closes")
    for px in (candle.open, candle.high, candle.low, candle.close):
        instrument.check_price(px)
    if candle.volume_native % instrument.quantity_step:
        raise MarketDataError("candle native volume violates contract quantity step")
    price_precision = _precision(instrument.tick_size)
    quantity_precision = _precision(instrument.quantity_step)
    return Bar(
        BarType.from_str(f"{nautilus_instrument_id(instrument)}-1-MINUTE-LAST-EXTERNAL"),
        Price.from_str(f"{candle.open:.{price_precision}f}"),
        Price.from_str(f"{candle.high:.{price_precision}f}"),
        Price.from_str(f"{candle.low:.{price_precision}f}"),
        Price.from_str(f"{candle.close:.{price_precision}f}"),
        Quantity.from_str(f"{candle.volume_native:.{quantity_precision}f}"),
        candle.end_ns,
        received_ns,
    )
