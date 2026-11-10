"""Research-only daily equity envelope retaining ambiguous funding settlements.

Two deterministic boundary scenarios include every trade and every certain
settlement. At ambiguous boundaries, the lower path includes possible
funding debits but not possible credits; the upper path does the converse.
The reported paths are NOT broker wallet statements, liquidation outcomes,
or independent historical experiments. No IO, networking or order placement.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from math import isclose, isfinite

import pandas as pd

from lab.bearish_ensemble import MINUTE_NS, RoundTripCosts
from lab.bearish_ensemble_replay import ShortReplay
from lab.bearish_equity import EquityAssumptions, _closeout, daily_short_equity
from lab.bearish_funding_audit import short_funding_bounds
from lab.crash_funding_ledger import ENTRY_UNCERTAINTY, EXIT_UNCERTAINTY

MODEL = "short-funding-uncertainty-daily-equity-v1"


@dataclass(frozen=True)
class IntervalDay:
    day: str
    lower_start_equity: float
    upper_start_equity: float
    lower_end_equity: float
    upper_end_equity: float
    lower_path_net_pnl: float
    upper_path_net_pnl: float
    lower_path_funding_cashflow: float
    upper_path_funding_cashflow: float
    open_position: bool


@dataclass(frozen=True)
class IntervalEquity:
    model: str
    source_plan_sha256: str
    source_events_sha256: str
    days: tuple[IntervalDay, ...]
    completed_trades: int
    certain_settlements: int
    ambiguous_settlements: int
    lower_end_equity: float
    upper_end_equity: float
    lower_total_return: float
    upper_total_return: float
    qualified: bool = False


@dataclass(frozen=True)
class _Action:
    time_ns: int
    priority: int
    trade_index: int
    rate_mark: float = 0.0


def _events(replay: ShortReplay, source: dict, bounds) -> tuple[_Action, ...]:
    """Create events for both scenarios without dropping uncertain positions.

    Credits/debits are recognizable at the settlement timestamp, but
    cannot precede a modeled entry. An uncertain post-exit settlement
    stays associated with the just-closed trade's fixed quantity.
    """
    actions: list[_Action] = []
    events = source["events"]
    times_ms = [event["fundingTime"] for event in events]
    for i, (trade, result) in enumerate(zip(replay.trades, bounds.trades, strict=True)):
        start, end = trade.entry_minute_start_ns, trade.exit_bar_end_ns
        actions.extend((_Action(start, 2, i), _Action(end, 0, i)))
        entry = pd.Timestamp(start, unit="ns", tz="UTC")
        exit_ = pd.Timestamp(end, unit="ns", tz="UTC")
        first_ms = int((entry - ENTRY_UNCERTAINTY).value // 1_000_000)
        last_ms = int((exit_ + ENTRY_UNCERTAINTY).value // 1_000_000)
        nearby = events[bisect_left(times_ms, first_ms) : bisect_right(times_ms, last_ms)]
        certain = ambiguous = 0
        for raw in nearby:
            when_ns = int(raw["fundingTime"]) * 1_000_000
            when = pd.Timestamp(when_ns, unit="ns", tz="UTC")
            if entry + ENTRY_UNCERTAINTY < when < exit_ - EXIT_UNCERTAINTY:
                certain += 1
                actions.append(
                    _Action(
                        max(start, when_ns),
                        3 if when_ns <= start else 1,
                        i,
                        float(raw["fundingRate"]) * float(raw["markPrice"]),
                    )
                )
            elif entry - ENTRY_UNCERTAINTY <= when <= exit_ + ENTRY_UNCERTAINTY:
                ambiguous += 1
                actions.append(
                    _Action(
                        max(start, when_ns),
                        3 if when_ns <= start else 1,
                        i,
                        float(raw["fundingRate"]) * float(raw["markPrice"]),
                    )
                )
        if certain != result.certain_settlements or ambiguous != result.ambiguous_settlements:
            raise ValueError("funding settlement classification differs from pinned audit")
    return tuple(sorted(actions, key=lambda a: (a.time_ns, a.priority, a.trade_index)))


def _scenario(
    frame: pd.DataFrame,
    replay: ShortReplay,
    costs: RoundTripCosts,
    assumptions: EquityAssumptions,
    bounds,
    actions: tuple[_Action, ...],
    *,
    upper: bool,
) -> list[tuple[float, float, float, bool]]:
    """One chronological possible cash/equity path, including zero-trade days."""
    cash = assumptions.initial_equity
    previous_equity = cash
    fee_rate = costs.fee_per_side_bps / 10_000
    quantities = [0.0] * len(replay.trades)
    per_trade_funding = [0.0] * len(replay.trades)
    active: int | None = None
    pointer = 0
    records: list[tuple[float, float, float, bool]] = []
    marks = frame.index[(frame.index.hour == 0) & (frame.index.minute == 0)]

    for mark in marks:
        mark_ns = int(mark.value)
        funding_today = 0.0
        while pointer < len(actions):
            action = actions[pointer]
            if action.time_ns > mark_ns or (action.time_ns == mark_ns and action.priority >= 2):
                break
            trade = replay.trades[action.trade_index]
            i = action.trade_index
            if action.priority == 2:
                if active is not None or cash <= 0:
                    raise ValueError("overlapping funded short or depleted equity")
                qty = cash * assumptions.notional_fraction / trade.entry_reference
                quantities[i] = qty
                cash -= qty * trade.entry_fill * fee_rate
                active = i
            elif action.priority in (1, 3):
                qty = quantities[i]
                if qty <= 0:
                    raise ValueError("funding event attributed before entry sizing")
                amount = qty * action.rate_mark
                entry = pd.Timestamp(trade.entry_minute_start_ns, unit="ns", tz="UTC")
                exit_ = pd.Timestamp(trade.exit_bar_end_ns, unit="ns", tz="UTC")
                event = pd.Timestamp(action.time_ns, unit="ns", tz="UTC")
                # Classification is made from the event's original time.
                # For events just before entry, the booked time was moved
                # to entry; their ownership is necessarily uncertain.
                certain = (
                    entry + ENTRY_UNCERTAINTY < event < exit_ - EXIT_UNCERTAINTY
                    and event.value >= trade.entry_minute_start_ns
                )
                if certain or (upper and amount > 0) or (not upper and amount < 0):
                    cash += amount
                    per_trade_funding[i] += amount
                    funding_today += amount
            else:
                if active != i:
                    raise ValueError("short exit missing its owned inventory")
                qty = quantities[i]
                cash += qty * (trade.entry_fill - trade.exit_fill)
                cash -= qty * trade.exit_fill * fee_rate
                active = None
            pointer += 1

        if active is not None:
            trade = replay.trades[active]
            qty = quantities[active]
            cover = _closeout(float(frame.loc[mark, "close"]), costs)
            equity = cash + qty * (trade.entry_fill - cover) - qty * cover * fee_rate
        else:
            equity = cash
        if not isfinite(cash) or not isfinite(equity) or equity <= 0 or cash <= 0:
            raise ValueError("scenario cash/equity depleted; liquidation not modeled")
        records.append((previous_equity, equity, funding_today, active is not None))
        previous_equity = equity

    if pointer != len(actions) or active is not None:
        raise ValueError("unprocessed settlement or terminal inventory")
    for i, (trade, result) in enumerate(zip(replay.trades, bounds.trades, strict=True)):
        if not isclose(
            per_trade_funding[i],
            quantities[i]
            * trade.entry_reference
            * (result.funding_upper_return if upper else result.funding_lower_return),
            rel_tol=1e-9,
            abs_tol=1e-7,
        ):
            raise ValueError("scenario cashflows fail funding-bound reconciliation")
        # OHLC high stress cannot establish executable liquidation. If the
        # scenario would breach the conservative equity floor, report no
        # account profit instead of silently accepting the selected trades.
        first = frame.index.get_loc(pd.Timestamp(trade.entry_minute_start_ns + MINUTE_NS, unit="ns", tz="UTC"))
        last = frame.index.get_loc(pd.Timestamp(trade.exit_bar_end_ns, unit="ns", tz="UTC"))
        cover = _closeout(float(frame["high"].iloc[first : last + 1].max()), costs)
        qty = quantities[i]
        # Conservatively account for all modeled trade funding debits.
        possible_debit = min(0.0, per_trade_funding[i])
        # Risk calculation uses the cash immediately after modeled entry,
        # reconstructed from the quantity and its fixed equity fraction.
        initial_equity = qty * trade.entry_reference / assumptions.notional_fraction
        stressed = (
            initial_equity
            + qty * (trade.entry_fill - cover)
            - qty * (trade.entry_fill + cover) * fee_rate
            + possible_debit
        )
        if stressed <= qty * trade.entry_reference * assumptions.minimum_equity_fraction:
            raise ValueError("funding interval breaches intrabar equity floor")
    return records


def interval_daily_short_equity(
    frame: pd.DataFrame,
    replay: ShortReplay,
    costs: RoundTripCosts,
    evidence: dict,
    *,
    boundary: pd.Timestamp,
    expected_plan_sha256: str,
    assumptions: EquityAssumptions | None = None,
    symbol: str = "BTCUSDT",
) -> IntervalEquity:
    """Return both consistent daily scenarios, never a selected-trade subset.

    Each scenario compounds independently with event-time settlements.
    Per-day scenario P&L is not itself an independently optimized daily
    confidence bound. Model remains RETROSPECTIVE_UNQUALIFIED.
    """
    a = assumptions or EquityAssumptions()
    reference = daily_short_equity(frame, replay, costs, boundary=boundary, assumptions=a)
    bounds = short_funding_bounds(
        replay, costs, evidence, boundary=boundary, expected_plan_sha256=expected_plan_sha256, symbol=symbol
    )
    actions = _events(replay, evidence, bounds)
    lower = _scenario(frame, replay, costs, a, bounds, actions, upper=False)
    upper = _scenario(frame, replay, costs, a, bounds, actions, upper=True)
    if not len(lower) == len(upper) == len(reference.days):
        raise ValueError("funding intervals are missing UTC dates")
    days = []
    for ref, lo, hi in zip(reference.days, lower, upper, strict=True):
        if lo[1] > hi[1] + 1e-7 or lo[3] != hi[3]:
            raise ValueError("funding equity scenarios crossed; interval unavailable")
        days.append(
            IntervalDay(
                day=ref.day,
                lower_start_equity=lo[0],
                upper_start_equity=hi[0],
                lower_end_equity=lo[1],
                upper_end_equity=hi[1],
                lower_path_net_pnl=lo[1] - lo[0],
                upper_path_net_pnl=hi[1] - hi[0],
                lower_path_funding_cashflow=lo[2],
                upper_path_funding_cashflow=hi[2],
                open_position=lo[3],
            )
        )
    end_lo = lower[-1][1]
    end_hi = upper[-1][1]
    return IntervalEquity(
        model=MODEL,
        source_plan_sha256=bounds.source_plan_sha256,
        source_events_sha256=bounds.source_events_sha256,
        days=tuple(days),
        completed_trades=len(replay.trades),
        certain_settlements=bounds.certain_settlements,
        ambiguous_settlements=bounds.ambiguous_settlements,
        lower_end_equity=end_lo,
        upper_end_equity=end_hi,
        lower_total_return=end_lo / a.initial_equity - 1,
        upper_total_return=end_hi / a.initial_equity - 1,
    )
