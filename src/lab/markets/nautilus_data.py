"""Typed read-only crypto data conversion into the *existing* Nautilus event model.

This is a market-data boundary, not a new strategy/risk/execution actor and
never constructs a broker order. A completed OHLCV candle is a Bar ONLY; an
actual observed public trade is a TradeTick. Neither is sufficient to infer
best bid/ask, queue fills, regional account capabilities or collateral.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nautilus_trader.model import (
    AggressorSide,
    Bar,
    BarType,
    InstrumentId,
    MarkPriceUpdate,
    Price,
    Quantity,
    QuoteTick,
    TradeId,
    TradeTick,
)

from lab.markets.book_sequence import SequencedBook
from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, AssetClass, ContractKind, Instrument, MarkPrice, Side, Trade
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
        aggressor = AggressorSide.BUY
    elif trade.taker_side == Side.SELL:
        aggressor = AggressorSide.SELL
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
    if type(received_ns) is not int or received_ns > MAX_EVENT_TIMESTAMP_NS:
        raise MarketDataError("invalid signed-64-bit candle receive timestamp")
    if received_ns < candle.end_ns:
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


def to_nautilus_mark(mark: MarkPrice, instrument: Instrument) -> MarkPriceUpdate:
    """Independent derivative mark: not an execution tick, quote, or candle.

    Unlike a last trade, Nautilus uses this class to cache margin-relevant
    valuation data. The caller must still validate the provenance and
    completeness of the original external mark feed independently.
    """
    # Mark/index price precision is independent of the *tradable* order tick.
    # Rounding the mark to an order tick would silently alter derivative PnL,
    # collateral and liquidation calculations.
    pp = _precision(mark.price)
    if pp > 9:
        raise MarketDataError("mark precision exceeds native fixed-price representation")
    return MarkPriceUpdate(
        instrument_id=nautilus_instrument_id(instrument),
        value=Price.from_str(f"{mark.price:.{pp}f}"),
        ts_event=mark.exchange_ns,
        ts_init=mark.received_ns,
    )


def to_nautilus_book_quote(book: SequencedBook, instrument: Instrument) -> QuoteTick:
    """Read-only native BBO from an offline sequenced frame, never a fill.

    Does not promote public REST snapshots or incomplete book data into
    execution-quality orders, maker queue position or available collateral.
    """
    if (
        type(book) is not SequencedBook
        or book.source != "offline_recorded_okx_books_ws"
        or book.execution_qualified
        or book.instrument_id != instrument.symbol
    ):
        raise MarketDataError("unqualified or mismatched recorded book source")
    if not book.bids or not book.asks or book.best_bid >= book.best_ask:
        raise MarketDataError("recorded quote requires a valid uncrossed book")
    for level in (book.bids[0], book.asks[0]):
        instrument.check_price(level.price)
        if level.size_native <= 0 or level.size_native % instrument.quantity_step:
            raise MarketDataError("book size has unqualified contract units or precision")
    pp = _precision(instrument.tick_size)
    qp = _precision(instrument.quantity_step)
    return QuoteTick(
        nautilus_instrument_id(instrument),
        Price.from_str(f"{book.best_bid:.{pp}f}"),
        Price.from_str(f"{book.best_ask:.{pp}f}"),
        Quantity.from_str(f"{book.bids[0].size_native:.{qp}f}"),
        Quantity.from_str(f"{book.asks[0].size_native:.{qp}f}"),
        book.exchange_ns,
        book.received_ns,
    )


@dataclass(frozen=True, slots=True)
class CausalNautilusTape:
    """Native read-only event tape; never an execution-qualified data catalog.

    Original receipt ordering is preserved. We never sort a late observation
    back into earlier strategy knowledge. Exchange-time regressions or gaps
    in receipt ordering are rejected, not repaired using future information.
    """

    instrument_id: str
    events: tuple[TradeTick | MarkPriceUpdate | QuoteTick, ...]
    last_received_ns: int
    execution_qualified: bool = False


def build_causal_tape(
    instrument: Instrument,
    observations: Sequence[Trade | MarkPrice | SequencedBook],
) -> CausalNautilusTape:
    """Convert independently recorded tape observations for Nautilus ingestion.

    Caller must supply real receive-time ordered observations. Historical
    snapshots downloaded at one later timestamp are NOT a genuine streaming
    tape; they cannot qualify market replay from their exchange timestamps.
    No synthetic prices, implied marks, missing trades or forward fills.
    """
    if not observations:
        raise ValueError("cannot build a causal tape without source observations")
    previous_event = -1
    previous_received = -1
    ids: set[tuple[str, str]] = set()
    result: list[TradeTick | MarkPriceUpdate | QuoteTick] = []
    for observation in observations:
        if type(observation) is Trade:
            identity = ("trade", observation.trade_id)
            converted = to_nautilus_trade(observation, instrument)
        elif type(observation) is MarkPrice:
            identity = ("mark", observation.mark_id)
            converted = to_nautilus_mark(observation, instrument)
        elif type(observation) is SequencedBook:
            identity = ("quote", f"{observation.seq_id}:{observation.exchange_ns}:{observation.received_ns}")
            converted = to_nautilus_book_quote(observation, instrument)
        else:
            raise TypeError("only trades, independent marks and offline sequenced quotes may enter a causal tape")
        if identity in ids:
            raise ValueError("duplicate event identity in causal tape")
        if observation.received_ns < previous_received:
            raise ValueError("receipt-time regression: never sort information backwards into the past")
        if observation.exchange_ns < previous_event:
            raise ValueError("exchange-time regression: source replay requires explicit late-event policy")
        ids.add(identity)
        previous_received = observation.received_ns
        previous_event = observation.exchange_ns
        result.append(converted)
    return CausalNautilusTape(instrument.instrument_id, tuple(result), previous_received)
