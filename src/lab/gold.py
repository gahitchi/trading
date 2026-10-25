"""Read-only daily London Gold ATR trend preview.

This module does not create broker orders. Gold cannot use the stock-only execution
ledger, session calendar, or instrument mapping in the current paper node.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal

SYMBOL = "XAUUSD"
SECURITY_TYPE = "CMDTY"
EXCHANGE = "SMART"
CURRENCY = "USD"
MIN_OUNCES = 1
PRICE_TICK = Decimal("0.001")
ALLOCATION = Decimal("0.01")
STOP_ATR_MULTIPLE = Decimal("2")
REWARD_RISK = Decimal("2")
FAST_DAYS = 50
SLOW_DAYS = 200
ATR_DAYS = 14


@dataclass(frozen=True)
class GoldBar:
    """A complete daily bar, including when it became available to the strategy."""

    end: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class GoldPreview:
    """Indicative levels; protection must be recalculated from the actual fill."""

    symbol: str
    bar_end: datetime
    ounces: int
    entry_reference_usd: Decimal
    stop_reference_usd: Decimal
    target_reference_usd: Decimal
    atr_usd: Decimal
    allocated_usd: Decimal
    notional_usd: Decimal


def _validate(bars: list[GoldBar], as_of: datetime, capital_usd: Decimal) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if not capital_usd.is_finite() or capital_usd <= 0:
        raise ValueError("capital_usd must be finite and positive")
    if len(bars) < SLOW_DAYS:
        raise ValueError(f"need {SLOW_DAYS} complete daily bars")
    previous = None
    for bar in bars:
        if (
            bar.end.tzinfo is None
            or bar.end.utcoffset() is None
            or bar.available_at.tzinfo is None
            or bar.available_at.utcoffset() is None
            or bar.available_at < bar.end
            or bar.available_at > as_of
            or (previous is not None and (bar.end <= previous or bar.end.date() == previous.date()))
        ):
            raise ValueError("bars must be ordered, complete, and available by as_of")
        if any(not p.is_finite() or p <= 0 for p in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError("gold prices must be finite and positive")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("invalid OHLC bar")
        previous = bar.end
    if as_of - bars[-1].available_at > timedelta(hours=24):
        raise ValueError("latest gold bar is stale")


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def gold_atr_trend_preview(bars: list[GoldBar], *, as_of: datetime, capital_usd: Decimal) -> GoldPreview | None:
    """Long after a closed-bar return above SMA50 while SMA50 exceeds SMA200.

    At most 1% of verified USD-equivalent equity is allocated. The indicative
    stop is 2 ATR(14) below the close and the target is twice that risk above it.
    Return None when there is no entry or one ounce exceeds the allocation.
    """
    _validate(bars, as_of, capital_usd)
    closes = [bar.close for bar in bars]
    fast_now = _mean(closes[-FAST_DAYS:])
    fast_previous = _mean(closes[-FAST_DAYS - 1 : -1])
    slow_now = _mean(closes[-SLOW_DAYS:])
    if not (fast_now > slow_now and closes[-2] <= fast_previous and closes[-1] > fast_now):
        return None

    recent = bars[-ATR_DAYS:]
    prior_closes = closes[-ATR_DAYS - 1 : -1]
    true_ranges = [
        max(bar.high - bar.low, abs(bar.high - prior), abs(bar.low - prior))
        for bar, prior in zip(recent, prior_closes, strict=True)
    ]
    atr = _mean(true_ranges)
    if atr <= 0:
        return None
    entry = closes[-1]
    allocated = capital_usd * ALLOCATION
    ounces = int(allocated // entry)
    if ounces < MIN_OUNCES:
        return None
    distance = STOP_ATR_MULTIPLE * atr
    stop = (entry - distance).quantize(PRICE_TICK, rounding=ROUND_DOWN)
    target = (entry + REWARD_RISK * (entry - stop)).quantize(PRICE_TICK, rounding=ROUND_UP)
    if stop <= 0:
        return None
    return GoldPreview(SYMBOL, bars[-1].end, ounces, entry, stop, target, atr, allocated, ounces * entry)
