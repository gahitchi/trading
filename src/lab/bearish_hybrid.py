"""Research-only, short-only impulse/pause/continuation hybrid (hypothesis v1).

Pure, deterministic closed-minute state machine. It never places orders or
claims an edge. The existing retired BTC strategies and holdouts are untouched.
Inputs are caller-verified; paper fills and margin are NOT modeled here.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from math import isfinite
from statistics import fmean, median
from typing import Literal

MINUTE_NS = 60_000_000_000
HOUR_NS = 60 * MINUTE_NS
YEAR_HOURS = 365 * 24


@dataclass(frozen=True)
class Bar:
    end_ns: int  # exact UTC minute END, never an open/intrabar tick
    open: float
    high: float
    low: float
    close: float
    quote_volume: float
    taker_buy_quote: float

    def check(self) -> None:
        v = (self.open, self.high, self.low, self.close, self.quote_volume, self.taker_buy_quote)
        if type(self.end_ns) is not int or self.end_ns <= 0 or self.end_ns % MINUTE_NS:
            raise ValueError("expected positive UTC minute-end timestamp")
        if any(not isfinite(x) for x in v) or min(v[:4]) <= 0:
            raise ValueError("nonfinite or nonpositive price/volume")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("inconsistent OHLC")
        if self.low > self.high or not 0 <= self.taker_buy_quote <= self.quote_volume:
            raise ValueError("inconsistent traded volume")


@dataclass(frozen=True)
class FundingEvent:
    settlement_ns: int
    published_ns: int
    rate: float  # decimal, positive means shorts receive


def trailing_funding_apr(events: tuple[FundingEvent, ...], asof_ns: int) -> tuple[float | None, int]:
    """Annualize only observed/published payments in trailing 72h.

    NOT a forecast or proof the settlement history is complete. Variable
    intervals are supported by summing actual events, not assuming 8h.
    """
    if type(asof_ns) is not int or asof_ns <= 0:
        raise ValueError("invalid funding as-of time")
    last = -1
    rates: list[float] = []
    for e in events:
        if (
            type(e.settlement_ns) is not int
            or type(e.published_ns) is not int
            or e.settlement_ns <= last
            or e.published_ns < e.settlement_ns
            or not isfinite(e.rate)
        ):
            raise ValueError("invalid/nonchronological funding event")
        last = e.settlement_ns
        if asof_ns - 72 * HOUR_NS < e.settlement_ns <= asof_ns and e.published_ns <= asof_ns:
            rates.append(e.rate)
    if not rates:
        return None, 0
    return sum(rates) * YEAR_HOURS / 72 * 100, len(rates)


@dataclass(frozen=True)
class OIObservation:
    previous_ns: int
    previous_value: float
    observed_ns: int  # when latest OI was publicly available, not the sample's nominal time
    value: float

    def change_pct(self, asof_ns: int, max_age_min: int) -> float:
        if (
            type(self.previous_ns) is not int
            or type(self.observed_ns) is not int
            or self.previous_ns >= self.observed_ns
            or self.observed_ns > asof_ns
            or asof_ns - self.observed_ns > max_age_min * MINUTE_NS
            or self.observed_ns - self.previous_ns > 60 * MINUTE_NS
            or self.observed_ns - self.previous_ns < 5 * MINUTE_NS
            or not all(isfinite(x) and x > 0 for x in (self.previous_value, self.value))
        ):
            raise ValueError("unavailable, stale or future open interest")
        return 100 * (self.value / self.previous_value - 1)


@dataclass(frozen=True)
class Costs:
    fee_per_side_bps: float
    slippage_per_side_bps: float
    full_spread_bps: float
    adverse_funding_bps: float  # caller-estimated adverse settlement exposure

    @property
    def round_trip_bps(self) -> float:
        vals = (
            self.fee_per_side_bps,
            self.slippage_per_side_bps,
            self.full_spread_bps,
            self.adverse_funding_bps,
        )
        if any(not isfinite(v) or v < 0 for v in vals):
            raise ValueError("invalid cost estimates")
        return 2 * (self.fee_per_side_bps + self.slippage_per_side_bps) + sum(vals[2:])


@dataclass(frozen=True)
class MarketEligibility:
    """Caller-verified listing and trailing turnover, observed as of this closed bar."""

    asof_ns: int
    spot_listed: bool
    usdm_listed: bool
    mean_daily_turnover_usdt_30d: float

    def check(self, bar_ns: int, min_turnover_usdt: float) -> None:
        if (
            type(self.asof_ns) is not int
            or not 0 <= bar_ns - self.asof_ns <= MINUTE_NS
            or type(self.spot_listed) is not bool
            or type(self.usdm_listed) is not bool
            or not isfinite(self.mean_daily_turnover_usdt_30d)
            or self.mean_daily_turnover_usdt_30d < 0
            or not (self.spot_listed and self.usdm_listed)
            or self.mean_daily_turnover_usdt_30d < min_turnover_usdt
        ):
            raise ValueError("unverified, stale, inactive or illiquid market")


@dataclass(frozen=True)
class VirtualAccount:
    """Simulated equity only; this structure carries NO actual money."""

    equity: float = 0.0
    day_start_equity: float = 0.0
    day_pnl: float = 0.0
    current_gross_notional: float = 0.0
    free_collateral: float = 0.0
    open_positions: int = 0
    kill_mode: Literal["run", "halt", "flatten"] = "run"
    risk_ready: bool = False

    def check(self) -> None:
        nums = (self.equity, self.day_start_equity, self.day_pnl, self.current_gross_notional, self.free_collateral)
        if any(not isfinite(x) for x in nums):
            raise ValueError("invalid virtual account numbers")
        if (
            min(self.equity, self.day_start_equity, self.current_gross_notional, self.free_collateral) < 0
            or type(self.open_positions) is not int
            or self.open_positions < 0
            or type(self.risk_ready) is not bool
            or self.kill_mode not in ("run", "halt", "flatten")
        ):
            raise ValueError("invalid virtual account state")


@dataclass(frozen=True)
class Risk:
    max_trade_loss_fraction: float = 0.0025  # 0.25% of paper account
    max_day_loss_fraction: float = 0.01  # 1% of start-of-day paper equity
    max_symbol_notional_fraction: float = 0.30
    max_portfolio_gross_fraction: float = 1.0
    margin_fraction: float = 0.25  # purely illustrative; not an exchange bracket
    stressed_rally_fraction: float = 0.20
    maintenance_fraction: float = 0.10  # illustrative, NOT exchange verified
    reserve_fraction: float = 0.05
    min_notional: float = 0.0  # specify real exchange minimum for a future execution study

    def __post_init__(self) -> None:
        vals = vars(self).values()
        if any(not isfinite(v) or v < 0 for v in vals) or self.max_day_loss_fraction > 1:
            raise ValueError("invalid risk limits")
        if self.max_trade_loss_fraction <= 0 or self.margin_fraction <= 0:
            raise ValueError("risk and initial margin must be positive")


def paper_size(a: VirtualAccount, risk: Risk, stop_bps: float, costs_bps: float) -> float:
    """Upper bound, never a filled quantity. Rally stress is conservative, not a liquidation simulation."""
    a.check()
    if not all(isfinite(v) and v >= 0 for v in (stop_bps, costs_bps)) or stop_bps <= 0:
        raise ValueError("invalid prospective stop/cost")
    if a.equity <= 0 or a.day_start_equity <= 0 or a.kill_mode != "run" or not a.risk_ready:
        return 0.0
    if a.open_positions or a.day_pnl <= -risk.max_day_loss_fraction * a.day_start_equity:
        return 0.0
    loss_left = risk.max_day_loss_fraction * a.day_start_equity + a.day_pnl
    cap_loss = min(risk.max_trade_loss_fraction * a.equity, max(0.0, loss_left))
    if cap_loss <= 0:
        return 0.0
    max_loss_ratio = (stop_bps + costs_bps) / 10_000
    margin_stress = max(
        risk.margin_fraction, risk.stressed_rally_fraction + risk.maintenance_fraction + risk.reserve_fraction
    )
    bound = min(
        cap_loss / max_loss_ratio,
        risk.max_symbol_notional_fraction * a.equity,
        max(0.0, risk.max_portfolio_gross_fraction * a.equity - a.current_gross_notional),
        a.free_collateral / margin_stress,
    )
    return bound if bound >= risk.min_notional else 0.0


@dataclass(frozen=True)
class Settings:
    impulse_bars: int = 15
    impulse_drop_bps: float = 80.0
    baseline_bars: int = 60
    impulse_volume_multiple: float = 1.8
    impulse_sell_share: float = 0.58
    confirmation_volume_multiple: float = 1.3
    confirmation_sell_share: float = 0.60
    atr_bars: int = 30
    min_pullback_atr: float = 0.25
    max_pullback_atr: float = 1.5
    min_wait_bars: int = 2
    max_wait_bars: int = 12
    break_buffer_bps: float = 2.0
    stop_buffer_bps: float = 3.0
    max_stop_bps: float = 180.0
    reward_to_risk: float = 2.0
    max_cost_bps: float = 35.0
    target_cost_multiple: float = 2.0
    cooldown_bars: int = 30
    max_oi_age_min: int = 15
    oi_rise_pct: float = 0.2
    oi_flush_pct: float = 1.0
    funding_boost_apr: float = 10.0
    min_30d_perp_daily_turnover_usdt: float = 20_000_000.0

    def __post_init__(self) -> None:
        ints = (
            self.impulse_bars,
            self.baseline_bars,
            self.atr_bars,
            self.min_wait_bars,
            self.max_wait_bars,
            self.cooldown_bars,
            self.max_oi_age_min,
        )
        if any(type(n) is not int or n <= 0 for n in ints) or self.max_wait_bars < self.min_wait_bars:
            raise ValueError("invalid sampling or state-machine windows")
        vs = [
            v
            for k, v in vars(self).items()
            if k
            not in (
                "impulse_bars",
                "baseline_bars",
                "atr_bars",
                "min_wait_bars",
                "max_wait_bars",
                "cooldown_bars",
                "max_oi_age_min",
                "funding_boost_apr",
            )
        ]
        if any(not isfinite(v) or v <= 0 for v in vs) or not isfinite(self.funding_boost_apr):
            raise ValueError("invalid signal parameters")
        if self.impulse_sell_share > 1 or self.confirmation_sell_share > 1:
            raise ValueError("invalid selling share")


@dataclass
class _Impulse:
    at_ns: int
    low: float
    close: float
    atr: float
    reaction_high: float
    pulled_back: bool = False


@dataclass(frozen=True)
class Decision:
    action: Literal["FLAT", "WATCH", "RESEARCH_SHORT", "UNFUNDED_SIGNAL"]
    at_ns: int
    reasons: tuple[str, ...]
    oi_regime: str = "unknown"
    funding_apr_72h: float | None = None
    funding_events: int = 0
    reference_entry: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    estimated_cost_bps: float | None = None
    priority: int = 0
    virtual_notional: float = 0.0
    actual_notional: float = 0.0  # intentionally never changes from zero


class BearishHybrid:
    """Pure detector. Caller is responsible for data validation, trial logs and virtual fills."""

    def __init__(self, settings: Settings | None = None, risk: Risk | None = None) -> None:
        self.s = settings or Settings()
        self.risk = risk or Risk()
        self._history: deque[Bar] = deque(
            maxlen=max(self.s.baseline_bars + self.s.impulse_bars + 1, self.s.atr_bars + 2)
        )
        self._impulse: _Impulse | None = None
        self._last_signal_ns: int | None = None

    def reset(self) -> None:
        self._history.clear()
        self._impulse = None
        self._last_signal_ns = None

    def _baseline(self) -> float | None:
        # Exclude impulse candles from reference, including the candidate's candle.
        prev = list(self._history)[: -self.s.impulse_bars]
        window = prev[-self.s.baseline_bars :]
        return median(b.quote_volume for b in window) if len(window) == self.s.baseline_bars else None

    def _atr(self) -> float | None:
        # ATR before the impulse; no current-impulse volatility used to shrink position size.
        prev = list(self._history)[: -self.s.impulse_bars]
        window = prev[-self.s.atr_bars - 1 :]
        if len(window) != self.s.atr_bars + 1:
            return None
        return fmean(max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in pairwise(window))

    def on_bar(
        self,
        bar: Bar,
        *,
        oi: OIObservation | None,
        market: MarketEligibility | None,
        funding: tuple[FundingEvent, ...],
        funding_verified: bool,
        costs: Costs,
        account: VirtualAccount,
    ) -> Decision:
        bar.check()
        account.check()
        total_cost = costs.round_trip_bps
        if type(funding_verified) is not bool:
            raise ValueError("funding_verified must be an explicit boolean")
        # Validate all funding events even in warmup; ignore unpublished events in the calculation.
        apr, event_count = trailing_funding_apr(funding, bar.end_ns)
        if self._history and bar.end_ns <= self._history[-1].end_ns:
            raise ValueError("out-of-order or duplicate bar")
        if self._history and bar.end_ns - self._history[-1].end_ns != MINUTE_NS:
            self.reset()
            self._history.append(bar)
            return Decision("FLAT", bar.end_ns, ("history_gap",), estimated_cost_bps=total_cost)
        self._history.append(bar)
        baseline = self._baseline()
        if baseline is None or baseline <= 0:
            return Decision("FLAT", bar.end_ns, ("warmup",), estimated_cost_bps=total_cost)
        if self._impulse is not None:
            p = self._impulse
            age = (bar.end_ns - p.at_ns) // MINUTE_NS
            if age > self.s.max_wait_bars:
                self._impulse = None
                return Decision("FLAT", bar.end_ns, ("pause_expired",), estimated_cost_bps=total_cost)
            p.reaction_high = max(p.reaction_high, bar.high)
            if p.reaction_high > p.low + p.atr * self.s.max_pullback_atr:
                self._impulse = None
                return Decision("FLAT", bar.end_ns, ("bounce_invalidated",), estimated_cost_bps=total_cost)
            # Pullback must be in a PREVIOUSLY COMPLETED candle. Otherwise
            # same-candle OHLC cannot establish high-before-low ordering.
            prior_pullback = p.pulled_back
            if bar.high >= p.low + p.atr * self.s.min_pullback_atr:
                p.pulled_back = True
            breaking = bar.close < p.low * (1 - self.s.break_buffer_bps / 10_000)
            sells = (
                bar.quote_volume > 0 and 1 - bar.taker_buy_quote / bar.quote_volume >= self.s.confirmation_sell_share
            )
            volume = bar.quote_volume >= self.s.confirmation_volume_multiple * baseline
            if not (
                age >= self.s.min_wait_bars
                and prior_pullback
                and breaking
                and bar.close < bar.open
                and sells
                and volume
            ):
                return Decision("WATCH", bar.end_ns, ("awaiting_renewed_breakdown",), estimated_cost_bps=total_cost)
            self._impulse = None
            vetoes: list[str] = []
            if (
                self._last_signal_ns is not None
                and bar.end_ns - self._last_signal_ns <= self.s.cooldown_bars * MINUTE_NS
            ):
                vetoes.append("cooldown")
            if total_cost > self.s.max_cost_bps:
                vetoes.append("friction_too_high")
            if market is None:
                vetoes.append("market_eligibility_missing")
            else:
                try:
                    market.check(bar.end_ns, self.s.min_30d_perp_daily_turnover_usdt)
                except ValueError:
                    vetoes.append("market_ineligible")
            oi_regime = "unknown"
            if oi is None:
                vetoes.append("oi_missing")
            else:
                try:
                    oi_change = oi.change_pct(bar.end_ns, self.s.max_oi_age_min)
                    if oi_change >= self.s.oi_rise_pct:
                        oi_regime = "build_up"
                    elif oi_change <= -self.s.oi_flush_pct:
                        oi_regime = "liquidation_flush"
                    else:
                        oi_regime = "neutral"
                except ValueError:
                    vetoes.append("oi_stale_or_invalid")
            if not funding_verified:
                vetoes.append("funding_not_verified")
            if event_count == 0:
                vetoes.append("funding_history_unavailable")
            stop = max(p.reaction_high, p.close) * (1 + self.s.stop_buffer_bps / 10_000)
            stop_bps = (stop / bar.close - 1) * 10_000
            target = bar.close - self.s.reward_to_risk * (stop - bar.close)
            if stop_bps > self.s.max_stop_bps or target <= 0:
                vetoes.append("unsafe_stop")
            if self.s.reward_to_risk * stop_bps < self.s.target_cost_multiple * total_cost:
                vetoes.append("insufficient_reward_vs_cost")
            virtual_notional = paper_size(account, self.risk, stop_bps, total_cost)
            priority = int(oi_regime == "build_up") + int(apr is not None and apr >= self.s.funding_boost_apr)
            if virtual_notional <= 0 and not (account.equity == 0 and account.kill_mode == "run"):
                vetoes.append("virtual_account_risk")
            if vetoes:
                return Decision(
                    "FLAT",
                    bar.end_ns,
                    tuple(vetoes),
                    oi_regime,
                    apr,
                    event_count,
                    estimated_cost_bps=total_cost,
                    priority=priority,
                )
            self._last_signal_ns = bar.end_ns
            if virtual_notional == 0:
                return Decision(
                    "UNFUNDED_SIGNAL",
                    bar.end_ns,
                    ("eligible_but_unfunded",),
                    oi_regime,
                    apr,
                    event_count,
                    bar.close,
                    stop,
                    target,
                    total_cost,
                    priority,
                    0.0,
                    0.0,
                )
            return Decision(
                "RESEARCH_SHORT",
                bar.end_ns,
                ("impulse_pause_breakdown",),
                oi_regime,
                apr,
                event_count,
                bar.close,
                stop,
                target,
                total_cost,
                priority,
                virtual_notional,
                0.0,
            )
        if self._last_signal_ns is not None and bar.end_ns - self._last_signal_ns <= self.s.cooldown_bars * MINUTE_NS:
            return Decision("FLAT", bar.end_ns, ("cooldown",), estimated_cost_bps=total_cost)
        bars = list(self._history)
        if len(bars) < self.s.baseline_bars + self.s.impulse_bars + 1:
            return Decision("FLAT", bar.end_ns, ("warmup",), estimated_cost_bps=total_cost)
        impulse = bars[-self.s.impulse_bars :]
        old = bars[-self.s.impulse_bars - 1]
        drop_bps = (1 - bar.close / old.close) * 10_000
        volume = sum(b.quote_volume for b in impulse)
        buy = sum(b.taker_buy_quote for b in impulse)
        if (
            drop_bps >= self.s.impulse_drop_bps
            and volume >= self.s.impulse_volume_multiple * baseline * self.s.impulse_bars
            and volume > 0
            and 1 - buy / volume >= self.s.impulse_sell_share
        ):
            atr = self._atr()
            if atr is not None and atr > 0:
                self._impulse = _Impulse(bar.end_ns, bar.low, bar.close, atr, bar.low)
                return Decision("WATCH", bar.end_ns, ("impulse_armed",), estimated_cost_bps=total_cost)
        return Decision("FLAT", bar.end_ns, ("no_impulse",), estimated_cost_bps=total_cost)
