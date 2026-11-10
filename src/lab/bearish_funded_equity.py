"""Research-only daily short equity with time-ordered, audited funding cashflows.

This is a separate stricter accounting path, not a modification to any
registered historical experiment. It deliberately refuses uncertain
settlement ownership rather than converting retrospective funding bounds
into fabricated settled cash. No broker, exchange, live keys or orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import isclose, isfinite

import pandas as pd

from lab.bearish_ensemble import MINUTE_NS, RoundTripCosts
from lab.bearish_ensemble_replay import ShortReplay
from lab.bearish_equity import EquityAssumptions, _closeout, daily_short_equity
from lab.bearish_funding_audit import short_funding_bounds
from lab.crash_funding_ledger import ENTRY_UNCERTAINTY, EXIT_UNCERTAINTY

MODEL = "short-audited-settlement-daily-equity-v1"


@dataclass(frozen=True)
class FundedDay:
    day: str
    start_equity: float
    end_equity: float
    net_pnl: float
    daily_return: float
    settled_cash_equity: float
    unrealized_after_closeout: float
    funding_cashflow: float
    funding_settlements: int
    entries: int
    exits: int
    open_position: bool


@dataclass(frozen=True)
class FundedEquity:
    model: str
    source_plan_sha256: str
    source_events_sha256: str
    assumptions: EquityAssumptions
    days: tuple[FundedDay, ...]
    completed_trades: int
    funded_settlements: int
    start_equity: float
    end_equity: float
    total_return: float
    max_drawdown: float
    positive_days: int
    negative_days: int
    flat_days: int
    qualified: bool = False


def funded_daily_short_equity(
    frame: pd.DataFrame,
    replay: ShortReplay,
    costs: RoundTripCosts,
    evidence: dict,
    *,
    boundary: pd.Timestamp,
    expected_plan_sha256: str,
    assumptions: EquityAssumptions | None = None,
    symbol: str = "BTCUSDT",
) -> FundedEquity:
    """Compute marked daily cash equity with only certainly owned settlements.

    Calls the strict full-minute/holdout/trade validator before any
    accounting. Assumes a single isolated short, no deposits or credit,
    no borrow, no margin tiers, and no authenticated executable marks.
    Funding ownership ambiguity is an ERROR, not a zero cashflow.

    All data may be retrospective, but every day is marked using only
    funding events timestamped at or before that UTC day boundary.
    Position sizing uses the settled reference-equity cash balance at
    each entry; unrealized profit cannot be reinvested.
    """
    a = assumptions or EquityAssumptions()
    # The existing fixed-reserve ledger is a VALIDATOR only: its daily
    # returns must not be reused or added to the funded ledger.
    reference = daily_short_equity(frame, replay, costs, boundary=boundary, assumptions=a)
    funding = short_funding_bounds(
        replay, costs, evidence, boundary=boundary, expected_plan_sha256=expected_plan_sha256, symbol=symbol
    )
    if funding.ambiguous_settlements:
        raise ValueError("ambiguous funding ownership: daily settled equity unavailable")
    trades = replay.trades
    if len(trades) != len(funding.trades):
        raise ValueError("funding and replay trade counts disagree")

    # Timed actions: same-instant exits close before a subsequent entry.
    # Settlement events cannot coincide with either fill when the funding
    # audit reports zero ambiguous events.
    actions: list[tuple[int, int, int, float]] = []
    for ident, trade in enumerate(trades):
        actions.append((trade.entry_minute_start_ns, 2, ident, 0.0))
        actions.append((trade.exit_bar_end_ns, 0, ident, 0.0))
        entry = pd.Timestamp(trade.entry_minute_start_ns, unit="ns", tz="UTC")
        exit_ = pd.Timestamp(trade.exit_bar_end_ns, unit="ns", tz="UTC")
        count = 0
        total_fraction = 0.0
        for event in evidence["events"]:
            when_ns = int(event["fundingTime"]) * 1_000_000
            when = pd.Timestamp(when_ns, unit="ns", tz="UTC")
            if entry + ENTRY_UNCERTAINTY < when < exit_ - EXIT_UNCERTAINTY:
                # Short receives positive funding, pays negative funding.
                rate_mark = float(event["fundingRate"]) * float(event["markPrice"])
                total_fraction += rate_mark / trade.entry_reference
                count += 1
                actions.append((when_ns, 1, ident, rate_mark))
        if count != funding.trades[ident].certain_settlements or not isclose(
            total_fraction, funding.trades[ident].funding_lower_return, rel_tol=1e-9, abs_tol=1e-11
        ):
            raise ValueError("certain funding cashflows fail pinned-return reconciliation")
    actions.sort()

    cash = float(a.initial_equity)
    previous_equity = cash
    peak = cash
    drawdown = 0.0
    rows: list[FundedDay] = []
    pointer = 0
    # id, quantity, original settled cash before entry, entry commission
    active: tuple[int, float, float, float] | None = None
    per_trade_funding = [0.0 for _ in trades]
    all_settlements = 0
    fee_rate = costs.fee_per_side_bps / 10_000

    # A candle ending at 00:00 UTC closes the PRECEDING calendar date.
    marks = frame.index[(frame.index.hour == 0) & (frame.index.minute == 0)]
    for mark in marks:
        mark_ns = int(mark.value)
        entries = exits = settlements = 0
        funding_cash = 0.0
        while pointer < len(actions):
            stamp, kind, ident, rate_mark = actions[pointer]
            # A new entry at exactly 00:00 belongs to the NEXT UTC day.
            # A funding settlement/exit at exactly 00:00 is posted now.
            if stamp > mark_ns or (stamp == mark_ns and kind == 2):
                break
            trade = trades[ident]
            if kind == 2:
                if active is not None or cash <= 0:
                    raise ValueError("invalid cash or overlapping short entry")
                original_cash = cash
                notional = original_cash * a.notional_fraction
                qty = notional / trade.entry_reference
                entry_fee = qty * trade.entry_fill * fee_rate
                cash -= entry_fee
                active = (ident, qty, original_cash, entry_fee)
                entries += 1
            elif kind == 1:
                if active is None or active[0] != ident:
                    raise ValueError("funding settlement without open short")
                value = active[1] * rate_mark
                cash += value
                per_trade_funding[ident] += value
                funding_cash += value
                settlements += 1
                all_settlements += 1
            else:
                if active is None or active[0] != ident:
                    raise ValueError("short exit without open inventory")
                _, qty, original_cash, entry_fee = active
                actual_funding = per_trade_funding[ident]
                expected = qty * trade.entry_reference * funding.trades[ident].funding_lower_return
                if not isclose(actual_funding, expected, rel_tol=1e-9, abs_tol=1e-8):
                    raise ValueError("settled cash differs from source-pinned funding return")
                exit_fee = qty * trade.exit_fill * fee_rate
                cash += qty * (trade.entry_fill - trade.exit_fill) - exit_fee
                # Research-only adverse high envelope. A funding debit is
                # fully charged; favorable uncertain funding is never used
                # to assert that a position survived an intrabar margin call.
                first = frame.index.get_loc(pd.Timestamp(trade.entry_minute_start_ns + MINUTE_NS, unit="ns", tz="UTC"))
                last = frame.index.get_loc(pd.Timestamp(trade.exit_bar_end_ns, unit="ns", tz="UTC"))
                worst_high = float(frame["high"].iloc[first : last + 1].max())
                cover = _closeout(worst_high, costs)
                stressed = (
                    original_cash
                    + qty * (trade.entry_fill - cover)
                    - entry_fee
                    - qty * cover * fee_rate
                    + min(0.0, actual_funding)
                )
                if stressed <= qty * trade.entry_reference * a.minimum_equity_fraction:
                    raise ValueError("funded intrabar equity floor breached; liquidation not modeled")
                if not isfinite(cash) or cash <= 0:
                    raise ValueError("funded account equity depleted; liquidation not modeled")
                active = None
                exits += 1
            pointer += 1
        if active is None:
            equity = cash
        else:
            ident, qty, _, _ = active
            cover = _closeout(float(frame.loc[mark, "close"]), costs)
            exit_fee = qty * cover * fee_rate
            equity = cash + qty * (trades[ident].entry_fill - cover) - exit_fee
        if not isfinite(equity) or equity <= 0:
            raise ValueError("nonpositive daily funded marked equity")
        day = (mark - timedelta(minutes=1)).date().isoformat()
        rows.append(
            FundedDay(
                day=day,
                start_equity=previous_equity,
                end_equity=equity,
                net_pnl=equity - previous_equity,
                daily_return=(equity - previous_equity) / previous_equity,
                settled_cash_equity=cash,
                unrealized_after_closeout=equity - cash,
                funding_cashflow=funding_cash,
                funding_settlements=settlements,
                entries=entries,
                exits=exits,
                open_position=active is not None,
            )
        )
        previous_equity = equity
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    if pointer != len(actions) or active is not None or len(rows) != len(reference.days):
        raise ValueError("terminal funding or short inventory remains unaccounted")
    if all_settlements != funding.certain_settlements:
        raise ValueError("settlement event count differs from source-pinned audit")
    return FundedEquity(
        model=MODEL,
        source_plan_sha256=funding.source_plan_sha256,
        source_events_sha256=funding.source_events_sha256,
        assumptions=a,
        days=tuple(rows),
        completed_trades=len(trades),
        funded_settlements=all_settlements,
        start_equity=a.initial_equity,
        end_equity=cash,
        total_return=cash / a.initial_equity - 1,
        max_drawdown=drawdown,
        positive_days=sum(row.net_pnl > 0 for row in rows),
        negative_days=sum(row.net_pnl < 0 for row in rows),
        flat_days=sum(row.net_pnl == 0 for row in rows),
    )
