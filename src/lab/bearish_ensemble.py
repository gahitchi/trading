"""Research-only, short-only one-minute signal ensemble.

All inputs are completed UTC minute bars. Decisions at bar end use only
that bar and earlier bars; entries/fills are deliberately NOT modeled here.
No keys, orders, backtest win claims or holdout access in this module.

Candidate v1 is frozen by source/parameters. Historical evaluation requires
its own pre-registered, audited research-family trial, fee/funding ledger,
and market-data-quality checks. Never convert a signal to a live order.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
from statistics import fmean, median
from typing import Literal

MINUTE_NS = 60_000_000_000


@dataclass(frozen=True)
class MinuteBar:
    end_ns: int  # UTC nanoseconds; the candle is CLOSED at this time
    open: float
    high: float
    low: float
    close: float
    quote_volume: float
    taker_buy_quote: float

    def validate(self) -> None:
        values = (self.open, self.high, self.low, self.close, self.quote_volume, self.taker_buy_quote)
        if not isinstance(self.end_ns, int) or self.end_ns <= 0 or self.end_ns % MINUTE_NS:
            raise ValueError("bar end must be a positive UTC minute-boundary nanosecond timestamp")
        if not all(isfinite(x) for x in values):
            raise ValueError("non-finite bar data")
        if min(values[:4]) <= 0 or self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid OHLC candle")
        if not 0 <= self.taker_buy_quote <= self.quote_volume:
            raise ValueError("invalid signed participation volumes")


@dataclass(frozen=True)
class RoundTripCosts:
    """Conservative bps estimates, not authenticated fill or fee evidence."""

    fee_per_side_bps: float
    slippage_per_side_bps: float
    spread_bps: float
    adverse_funding_bps: float = 0.0

    @property
    def total_bps(self) -> float:
        vals = (
            self.fee_per_side_bps,
            self.slippage_per_side_bps,
            self.spread_bps,
            self.adverse_funding_bps,
        )
        if not all(isfinite(x) and x >= 0 for x in vals):
            raise ValueError("round-trip cost estimates must be finite and non-negative")
        return 2 * (self.fee_per_side_bps + self.slippage_per_side_bps) + self.spread_bps + self.adverse_funding_bps


@dataclass(frozen=True)
class EnsembleSettings:
    fast_ema: int = 20
    slow_ema: int = 60
    momentum_min_bps: float = 25.0
    momentum_bars: int = 5
    support_bars: int = 30
    continuation_bars: int = 5
    break_buffer_bps: float = 2.0
    volume_bars: int = 60
    volume_multiple: float = 1.5
    sell_share: float = 0.58
    atr_bars: int = 30
    stop_atr_multiple: float = 1.5
    reward_to_risk: float = 2.0
    max_extension_atr: float = 3.0
    max_stop_bps: float = 180.0
    max_cost_bps: float = 35.0
    min_target_cost_multiple: float = 2.0
    cooldown_bars: int = 30

    def __post_init__(self) -> None:
        ints = (
            self.fast_ema,
            self.slow_ema,
            self.momentum_bars,
            self.support_bars,
            self.continuation_bars,
            self.volume_bars,
            self.atr_bars,
            self.cooldown_bars,
        )
        if any(type(v) is not int or v < 1 for v in ints) or self.fast_ema >= self.slow_ema:
            raise ValueError("window sizes must be positive integers; fast EMA must be shorter")
        values = (
            self.momentum_min_bps,
            self.break_buffer_bps,
            self.volume_multiple,
            self.sell_share,
            self.stop_atr_multiple,
            self.reward_to_risk,
            self.max_extension_atr,
            self.max_stop_bps,
            self.max_cost_bps,
            self.min_target_cost_multiple,
        )
        if not all(isfinite(v) and v > 0 for v in values) or not 0 < self.sell_share <= 1:
            raise ValueError("ensemble thresholds must be finite and positive")


@dataclass(frozen=True)
class EnsembleDecision:
    action: Literal["FLAT", "SHORT_CANDIDATE"]
    signal_end_ns: int
    gates_passed: tuple[str, ...]
    vetoes: tuple[str, ...]
    setup: str | None = None
    stop_bps: float | None = None
    target_bps: float | None = None
    estimated_cost_bps: float | None = None


class BearishEnsemble:
    """Single-symbol decision engine; caller owns positions, risk, and execution.

    The engine can only propose a SHORT_CANDIDATE, never a buy/long.
    Every data gap destroys warm-up state; no interpolated history is used.
    """

    def __init__(self, settings: EnsembleSettings | None = None):
        self.settings = settings or EnsembleSettings()
        s = self.settings
        self._warmup = max(
            3 * s.slow_ema, s.volume_bars + 1, s.support_bars + 1, s.atr_bars + 2, s.momentum_bars + 1
        )
        self._history: deque[MinuteBar] = deque(maxlen=self._warmup + 1)
        self._last_signal_ns: int | None = None

    def reset(self) -> None:
        self._history.clear()
        self._last_signal_ns = None

    @staticmethod
    def _ema(values: list[float], n: int) -> float:
        alpha = 2 / (n + 1)
        e = values[0]
        for v in values[1:]:
            e += alpha * (v - e)
        return e

    def on_closed_bar(
        self, bar: MinuteBar, costs: RoundTripCosts, *, flat: bool, risk_ready: bool
    ) -> EnsembleDecision:
        """Evaluate one complete bar. No external trade or risk side effects."""
        bar.validate()
        if type(flat) is not bool or type(risk_ready) is not bool:
            raise ValueError("flat and risk_ready must be explicit booleans")
        total_cost = costs.total_bps  # fail closed on missing/invalid costs
        if self._history and bar.end_ns <= self._history[-1].end_ns:
            raise ValueError("duplicate or out-of-order closed candle")
        if self._history and bar.end_ns - self._history[-1].end_ns != MINUTE_NS:
            self.reset()
            self._history.append(bar)
            return EnsembleDecision("FLAT", bar.end_ns, (), ("history_gap",), estimated_cost_bps=total_cost)
        self._history.append(bar)
        if len(self._history) < self._warmup + 1:
            return EnsembleDecision("FLAT", bar.end_ns, (), ("warmup",), estimated_cost_bps=total_cost)

        s = self.settings
        prior = list(self._history)[:-1]
        closes = [b.close for b in self._history]
        fast = self._ema(closes, s.fast_ema)
        slow = self._ema(closes, s.slow_ema)
        trending = fast < slow and bar.close < fast
        momentum = (bar.close / prior[-s.momentum_bars].close - 1) * 10_000 <= -s.momentum_min_bps
        support = min(b.low for b in prior[-s.support_bars:])
        minor_support = min(b.low for b in prior[-s.continuation_bars:])
        breakdown = bar.close < support * (1 - s.break_buffer_bps / 10_000)
        continuation = bar.close < minor_support * (1 - s.break_buffer_bps / 10_000)
        volume_baseline = median(b.quote_volume for b in prior[-s.volume_bars:])
        volume_ok = volume_baseline > 0 and bar.quote_volume >= s.volume_multiple * volume_baseline
        sell_ok = bar.quote_volume > 0 and (1 - bar.taker_buy_quote / bar.quote_volume) >= s.sell_share
        body_ok = bar.close < bar.open

        atr_inputs = prior[-s.atr_bars - 1:]
        ranges = [
            max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
            for p, b in zip(atr_inputs, atr_inputs[1:])
        ]
        atr = fmean(ranges)
        stop_bps = s.stop_atr_multiple * atr / bar.close * 10_000 if atr > 0 else 0.0
        target_bps = stop_bps * s.reward_to_risk
        extension_ok = atr > 0 and (fast - bar.close) <= s.max_extension_atr * atr
        stop_ok = 0 < stop_bps <= s.max_stop_bps
        cost_ok = total_cost <= s.max_cost_bps and target_bps >= s.min_target_cost_multiple * total_cost

        gates = {
            "bearish_regime": trending,
            "downside_momentum": momentum,
            "breakdown_or_continuation": breakdown or continuation,
            "sell_participation": volume_ok and sell_ok and body_ok,
            "risk_reward_after_costs": extension_ok and stop_ok and cost_ok,
        }
        vetoes = [name for name, passed in gates.items() if not passed]
        if not flat:
            vetoes.append("position_open")
        if not risk_ready:
            vetoes.append("risk_not_ready")
        if self._last_signal_ns is not None and bar.end_ns - self._last_signal_ns <= s.cooldown_bars * MINUTE_NS:
            vetoes.append("cooldown")
        passed = tuple(name for name, ok in gates.items() if ok)
        if vetoes:
            return EnsembleDecision("FLAT", bar.end_ns, passed, tuple(vetoes), estimated_cost_bps=total_cost)
        self._last_signal_ns = bar.end_ns
        return EnsembleDecision(
            "SHORT_CANDIDATE",
            bar.end_ns,
            passed,
            (),
            "breakdown" if breakdown else "continuation",
            stop_bps,
            target_bps,
            total_cost,
        )
