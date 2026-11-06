"""Read-only daily performance scorecard from a verified research ProfitJournal.

No strategy signals, exchange data, broker evidence, order placement,
profit posting, live risk authorization, or invented zero-trade returns.
The result is valid as *research-ledger accounting*, NOT proof of real P&L.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from statistics import median

from lab.profit_allocation import ProfitAllocationLedger
from lab.profit_journal import ProfitJournal

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


def _str(value: Decimal) -> str:
    return str(value)


def _pct(numerator: Decimal, denominator: Decimal) -> str | None:
    return str(numerator / denominator * _HUNDRED) if denominator > 0 else None


def daily_scorecard(journal: ProfitJournal, through: date) -> dict:
    """Replay and account for every UTC close, even with no trades.

    Actual equity includes external deposits/withdrawals. A separate
    flow-neutral curve is initial capital + accumulated daily net P&L
    and is used for *diagnostic* drawdown, not a live risk calculation.
    """
    if not isinstance(journal, ProfitJournal):
        raise ValueError("daily scorecard requires a verified profit journal")
    if type(through) is not date:
        raise ValueError("scorecard end date must be an exact date")

    # events() verifies journal hash chain, checkpoint and replay while
    # holding the journal lock, and returns a frozen event sequence.
    # Applying that one sequence ourselves avoids a multi-read race.
    source = journal.events()
    ledger = ProfitAllocationLedger(journal.policy, journal.start_day)
    trades_by_day: Counter[date] = Counter()
    fees_by_day: dict[date, Decimal] = {}
    slippage_by_day: dict[date, Decimal] = {}
    trade_funding_by_day: dict[date, Decimal] = {}
    cash_adjustments_by_day: dict[date, Decimal] = {}
    funding_adjustments_by_day: dict[date, Decimal] = {}
    for item in source:
        ledger.apply(item)
        if item.kind == "trade":
            day = item.timestamp.date()
            trades_by_day[day] += 1
            fees_by_day[day] = fees_by_day.get(day, _ZERO) + Decimal(item.fees)
            slippage_by_day[day] = slippage_by_day.get(day, _ZERO) + Decimal(item.slippage)
            trade_funding_by_day[day] = trade_funding_by_day.get(day, _ZERO) + Decimal(item.funding)
        elif item.kind == "adjustment":
            day = item.timestamp.date()
            cash_adjustments_by_day[day] = cash_adjustments_by_day.get(day, _ZERO) + Decimal(item.amount)
            if item.reason == "funding":
                funding_adjustments_by_day[day] = funding_adjustments_by_day.get(day, _ZERO) + Decimal(item.amount)

    # Blocks unclosed trailing trades, skipped calendar days, malformed
    # events, and last-day unvalued positions. Never pad missing closes.
    base = ledger.report(through)
    days = ledger.daily
    if not days or days[-1].day != through:
        raise ValueError("scorecard has incomplete closed-day coverage")
    if len(days) != base["days"]:
        raise ValueError("scorecard daily accounting row count mismatch")

    running_pnl = _ZERO
    running_external = _ZERO
    initial = Decimal(journal.policy.initial_capital)
    flow_neutral_peak = initial
    worst_drawdown = _ZERO
    worst_drawdown_pct: Decimal | None = _ZERO
    underwater = 0
    max_underwater = 0
    recovered_underwater_runs: list[int] = []
    max_inactive = 0
    inactive = 0
    samples: list[dict] = []
    positive_days = []
    negative_days = []
    eligible_returns: list[Decimal] = []
    unavailable_returns = 0
    min_day: Decimal | None = None
    fee_total = _ZERO
    slip_total = _ZERO
    trade_funding_total = _ZERO
    funding_adjustment_total = _ZERO
    total_trade_count = 0

    for row in days:
        day = row.day
        trades = trades_by_day.get(day, 0)
        total_trade_count += trades
        if trades:
            inactive = 0
        else:
            inactive += 1
            max_inactive = max(max_inactive, inactive)
        running_pnl += row.net_pnl
        running_external += row.external_flows
        neutral_equity = initial + running_pnl
        if row.end_equity != neutral_equity + running_external:
            raise ValueError("flow-adjusted daily equity fails cash conservation")
        if row.net_pnl > 0:
            positive_days.append(row.net_pnl)
        elif row.net_pnl < 0:
            negative_days.append(row.net_pnl)
        if min_day is None or row.net_pnl < min_day:
            min_day = row.net_pnl
        ret = _pct(row.net_pnl, row.start_equity)
        if ret is None:
            unavailable_returns += 1
        else:
            eligible_returns.append(Decimal(ret))
        if neutral_equity < flow_neutral_peak:
            underwater += 1
            max_underwater = max(max_underwater, underwater)
        else:
            if underwater:
                recovered_underwater_runs.append(underwater)
                underwater = 0
            flow_neutral_peak = neutral_equity
        drawdown = flow_neutral_peak - neutral_equity
        if drawdown > worst_drawdown:
            worst_drawdown = drawdown
        if flow_neutral_peak > 0:
            pct = drawdown / flow_neutral_peak * _HUNDRED
            if worst_drawdown_pct is not None:
                worst_drawdown_pct = max(worst_drawdown_pct, pct)
        else:
            worst_drawdown_pct = None
        fees = fees_by_day.get(day, _ZERO)
        slip = slippage_by_day.get(day, _ZERO)
        trade_funding = trade_funding_by_day.get(day, _ZERO)
        funding_adjustment = funding_adjustments_by_day.get(day, _ZERO)
        fee_total += fees
        slip_total += slip
        trade_funding_total += trade_funding
        funding_adjustment_total += funding_adjustment
        samples.append(
            {
                "day": day.isoformat(),
                "net_pnl": _str(row.net_pnl),
                "realized_net_pnl": _str(row.realized_net_pnl),
                "unrealized_change": _str(row.unrealized_change),
                "external_flows": _str(row.external_flows),
                "start_equity": _str(row.start_equity),
                "end_equity": _str(row.end_equity),
                "flow_neutral_equity": _str(neutral_equity),
                "daily_return_on_start_equity_pct": ret,
                "cumulative_net_pnl": _str(running_pnl),
                "trade_event_count": trades,
                "zero_trade_day": trades == 0,
                "trade_fees": _str(fees),
                "trade_slippage": _str(slip),
                "trade_funding_included": _str(trade_funding),
                "standalone_funding_adjustments": _str(funding_adjustment),
                "cash_adjustments_including_funding": _str(cash_adjustments_by_day.get(day, _ZERO)),
                "reinvested_realized_profit": _str(row.reinvested_profit),
                "retained_realized_profit": _str(row.retained_profit),
                "deployable": _str(row.deployable),
                "reserve": _str(row.reserve),
                "open_unrealized": _str(row.open_unrealized),
                "flow_neutral_drawdown": _str(drawdown),
            }
        )

    if sum((r.net_pnl for r in days), _ZERO) != running_pnl or running_pnl != Decimal(base["net_pnl"]):
        raise ValueError("reported and independently aggregated daily net P&L disagree")
    if len(positive_days) != base["winning_days"] or len(negative_days) != base["losing_days"]:
        raise ValueError("daily win/loss counts are inconsistent")
    active_days = sum(row["trade_event_count"] > 0 for row in samples)
    sorted_day_pnl = sorted(row.net_pnl for row in days)
    # Nearest-rank empirical 5th percentile: minimum for n < 20.
    p05 = sorted_day_pnl[max(0, (5 * len(days) + 99) // 100 - 1)]
    return {
        "status": "research_accounting_scorecard_unqualified",
        "source_kind": "verified_local_profit_journal",
        "currency": journal.policy.currency,
        "start_day": journal.start_day.isoformat(),
        "through_day": through.isoformat(),
        "reinvestment_fraction": journal.policy.reinvest_fraction,
        "initial_capital": journal.policy.initial_capital,
        "total_days": len(days),
        "winning_days": len(positive_days),
        "losing_days": len(negative_days),
        "flat_days": base["flat_days"],
        "active_trade_days": active_days,
        "zero_trade_days": len(days) - active_days,
        "max_consecutive_zero_trade_days": max_inactive,
        "trade_events": total_trade_count,
        "trades_per_calendar_day": str(Decimal(total_trade_count) / Decimal(len(days))),
        "trades_per_active_day": str(Decimal(total_trade_count) / Decimal(active_days)) if active_days else None,
        "mean_daily_net_pnl": _str(running_pnl / Decimal(len(days))),
        "median_daily_net_pnl": _str(median([row.net_pnl for row in days])),
        "p05_daily_net_pnl_nearest_rank": _str(p05),
        "largest_loss_day_net_pnl": _str(min_day),
        "gross_winning_day_gains": base["gross_winning_days"],
        "absolute_losing_day_losses": base["gross_losing_days_abs"],
        "profit_factor": base["profit_factor"],
        "mean_daily_return_on_start_equity_pct": (
            _str(sum(eligible_returns, _ZERO) / Decimal(len(eligible_returns))) if eligible_returns else None
        ),
        "median_daily_return_on_start_equity_pct": _str(median(eligible_returns)) if eligible_returns else None,
        "unavailable_daily_return_denominators": unavailable_returns,
        "total_net_pnl": base["net_pnl"],
        "actual_end_equity": _str(days[-1].end_equity),
        "cumulative_external_flows": _str(running_external),
        "flow_neutral_end_equity": _str(initial + running_pnl),
        "flow_neutral_max_drawdown": _str(worst_drawdown),
        "flow_neutral_max_drawdown_pct": _str(worst_drawdown_pct) if worst_drawdown_pct is not None else None,
        "max_underwater_calendar_days": max_underwater,
        "max_completed_recovery_calendar_days": max(recovered_underwater_runs, default=None),
        "current_unrecovered_calendar_days": underwater,
        "total_recorded_trade_fees": _str(fee_total),
        "total_recorded_additional_slippage": _str(slip_total),
        "trade_funding_already_included_in_trade_net": _str(trade_funding_total),
        "standalone_funding_adjustments": _str(funding_adjustment_total),
        "final_deployable": _str(days[-1].deployable),
        "final_reserve": _str(days[-1].reserve),
        "latest_risk_capacity": base["risk_capacity"],
        "daily": samples,
        "trade_fill_execution_quality": None,
        "latency_stress_results": None,
        "margin_liquidation_audit": None,
        "source_authenticated": False,
        "daily_edge_qualified": False,
        "risk_ready": False,
        "economic_posting_allowed": False,
    }
