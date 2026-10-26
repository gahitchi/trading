"""Closed daily-bar London Gold signal and PAPER sizing.

The levels are a plan, not orders: a broker fill can differ from the reference
close, and an execution client must attach and reconcile protective exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from itertools import pairwise

MIN_OUNCES = 1
ALLOCATION = Decimal("0.01")
FAST = 50
SLOW = 200
ATR_PERIOD = 14
STOP_ATR = Decimal("2")
REWARD_RISK = Decimal("2")


@dataclass(frozen=True)
class GoldBar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class GoldPlan:
    session: date
    quantity_oz: int
    reference_usd: Decimal
    stop_usd: Decimal
    target_usd: Decimal
    atr_usd: Decimal
    allocated_usd: Decimal
    notional_usd: Decimal
    minimum_lot_override: bool


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def plan_gold_trade(
    bars: list[GoldBar], *, equity_usd: Decimal, price_tick_usd: Decimal, min_quantity_oz: int = MIN_OUNCES
) -> GoldPlan | None:
    """Long on a closed-bar return above SMA50 while SMA50 exceeds SMA200.

    Exactly 1% of verified USD equity sets the normal notional budget. If a
    single broker minimum lot exceeds that budget, the operator's explicit
    minimum-lot exception is used and flagged on the returned plan.
    """
    if not equity_usd.is_finite() or equity_usd <= 0:
        raise ValueError("verified USD equity must be positive")
    if not price_tick_usd.is_finite() or price_tick_usd <= 0:
        raise ValueError("price tick must be positive")
    if min_quantity_oz < 1:
        raise ValueError("minimum gold quantity must be at least one ounce")
    if len(bars) < SLOW + 1:
        raise ValueError(f"need at least {SLOW + 1} closed daily bars")
    if any(b.session >= n.session for b, n in pairwise(bars)):
        raise ValueError("gold bars must be in strict session order")
    for bar in bars:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not p.is_finite() or p <= 0 for p in prices):
            raise ValueError("gold OHLC prices must be finite and positive")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("invalid gold OHLC range")
    closes = [b.close for b in bars]
    sma_fast = _mean(closes[-FAST:])
    sma_fast_prior = _mean(closes[-FAST - 1 : -1])
    sma_slow = _mean(closes[-SLOW:])
    if not (sma_fast > sma_slow and closes[-2] <= sma_fast_prior and closes[-1] > sma_fast):
        return None
    ranges = [
        max(b.high - b.low, abs(b.high - prior), abs(b.low - prior))
        for b, prior in zip(bars[-ATR_PERIOD:], closes[-ATR_PERIOD - 1 : -1], strict=True)
    ]
    atr = _mean(ranges)
    if atr <= 0:
        return None
    reference = closes[-1]
    allocated = equity_usd * ALLOCATION
    normal_qty = int(allocated // reference)
    quantity = max(min_quantity_oz, normal_qty)
    override = normal_qty < min_quantity_oz
    stop = ((reference - STOP_ATR * atr) / price_tick_usd).to_integral_value(rounding=ROUND_DOWN)
    stop *= price_tick_usd
    if stop <= 0:
        return None
    target = ((reference + REWARD_RISK * (reference - stop)) / price_tick_usd).to_integral_value(rounding=ROUND_UP)
    target *= price_tick_usd
    return GoldPlan(
        bars[-1].session,
        quantity,
        reference,
        stop,
        target,
        atr,
        allocated,
        quantity * reference,
        override,
    )
