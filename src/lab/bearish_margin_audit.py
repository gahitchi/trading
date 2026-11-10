"""Conservative research-only margin-envelope audit of modeled shorts.

No actual exchange liquidation tiers, maintenance brackets, collateral
haircuts, insurance rules, liquidation fees, or account-level cross-margin
state. This diagnostic uses realized equity and adverse intrabar OHLC highs;
a breach fails closed rather than simulating an exit price.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import pandas as pd

from lab.bearish_ensemble import MINUTE_NS, RoundTripCosts
from lab.bearish_ensemble_replay import ShortReplay
from lab.bearish_equity import EquityAssumptions, _closeout, daily_short_equity

MODEL = "short-intrabar-maintenance-envelope-v1"


@dataclass(frozen=True)
class MarginPolicy:
    initial_margin_fraction: float = 0.10
    maintenance_margin_fraction: float = 0.05
    liquidation_buffer_bps: float = 10.0

    def __post_init__(self) -> None:
        vals = (
            self.initial_margin_fraction,
            self.maintenance_margin_fraction,
            self.liquidation_buffer_bps,
        )
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not isfinite(v) for v in vals):
            raise ValueError("margin policy requires finite real values")
        if not 0 < self.maintenance_margin_fraction <= self.initial_margin_fraction <= 1:
            raise ValueError("maintenance margin must be positive and not exceed initial margin")
        if not 0 <= self.liquidation_buffer_bps < 10_000:
            raise ValueError("liquidation buffer must be in [0, 10000) bps")


@dataclass(frozen=True)
class MarginTrade:
    entry_minute_start_ns: int
    exit_bar_end_ns: int
    reference_notional: float
    worst_high: float
    minimum_stressed_equity: float
    stressed_maintenance_requirement: float
    maintenance_excess: float


@dataclass(frozen=True)
class MarginReport:
    model: str
    trades: tuple[MarginTrade, ...]
    minimum_maintenance_excess: float | None
    qualified: bool = False


def margin_envelope(
    frame: pd.DataFrame,
    replay: ShortReplay,
    costs: RoundTripCosts,
    *,
    boundary: pd.Timestamp,
    assumptions: EquityAssumptions | None = None,
    policy: MarginPolicy | None = None,
) -> MarginReport:
    """Fail closed when a hypothetical adverse candle high breaches maintenance.

    Runs the full closed-day equity validator first; the risk envelope is
    retrospective stress using the full trade lifetime, not a forecast
    available when entering. No liquidations are fabricated and no
    winning trades are selected by discarding negative stress cases.
    """
    a = assumptions or EquityAssumptions()
    m = policy or MarginPolicy()
    daily_short_equity(frame, replay, costs, boundary=boundary, assumptions=a)
    base = float(a.initial_equity)
    output = []
    for trade in replay.trades:
        notional = base * a.notional_fraction
        quantity = notional / trade.entry_reference
        initial_fee = quantity * trade.entry_fill * costs.fee_per_side_bps / 10_000
        initial_margin = notional * m.initial_margin_fraction
        if base - initial_fee <= initial_margin:
            raise ValueError("insufficient initial margin under conservative policy")

        entry_end = trade.entry_minute_start_ns + MINUTE_NS
        entry = pd.Timestamp(entry_end, unit="ns", tz="UTC")
        exit_ = pd.Timestamp(trade.exit_bar_end_ns, unit="ns", tz="UTC")
        # Both stamps and all intervening candles were validated by daily_short_equity.
        start_i = frame.index.get_loc(entry)
        end_i = frame.index.get_loc(exit_)
        highest = float(frame["high"].iloc[start_i : end_i + 1].max())
        cover = _closeout(highest, costs)
        exit_fee = quantity * cover * costs.fee_per_side_bps / 10_000
        funding_reserve = notional * trade.funding_reserve_cost
        stressed_equity = base + quantity * (trade.entry_fill - cover) - initial_fee - exit_fee - funding_reserve
        requirement = quantity * highest * (m.maintenance_margin_fraction + m.liquidation_buffer_bps / 10_000)
        excess = stressed_equity - requirement
        if not all(isfinite(v) for v in (stressed_equity, requirement, excess)):
            raise ValueError("nonfinite margin stress accounting")
        if excess <= 0:
            raise ValueError("maintenance margin breached in adverse OHLC envelope")
        output.append(
            MarginTrade(
                entry_minute_start_ns=trade.entry_minute_start_ns,
                exit_bar_end_ns=trade.exit_bar_end_ns,
                reference_notional=notional,
                worst_high=highest,
                minimum_stressed_equity=stressed_equity,
                stressed_maintenance_requirement=requirement,
                maintenance_excess=excess,
            )
        )
        base += notional * trade.net_return
        if base <= 0 or not isfinite(base):
            raise ValueError("account equity depleted under margin scenario")
    return MarginReport(
        model=MODEL,
        trades=tuple(output),
        minimum_maintenance_excess=min((row.maintenance_excess for row in output), default=None),
    )
