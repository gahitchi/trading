"""Source-pinned conservative funding bounds for already-modeled short trades.

Pure research adapter: no market-data access, API calls, registration,
trading decisions or paper/live orders. Source evidence must have first
passed crash_funding.load_verified; this function additionally checks
the pinned plan/event hashes and complete pre-holdout coverage. The
result is a return interval per trade, never a marked-equity series.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import timedelta
from math import isfinite

import pandas as pd

from lab import crash_funding
from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_replay import ShortReplay
from lab.bearish_equity import _reconcile
from lab.crash_funding_ledger import ENTRY_UNCERTAINTY, funding_bounds

MODEL = "bearish-source-pinned-funding-bounds-v1"


@dataclass(frozen=True)
class FundedShort:
    entry_minute_start_ns: int
    exit_bar_end_ns: int
    original_net_return: float
    no_reserve_net_return: float
    funding_lower_return: float
    funding_upper_return: float
    net_lower_return: float
    net_upper_return: float
    certain_settlements: int
    ambiguous_settlements: int


@dataclass(frozen=True)
class FundingReport:
    model: str
    source_plan_sha256: str
    source_events_sha256: str
    boundary: str
    trades: tuple[FundedShort, ...]
    lower_net_sum_not_portfolio: float
    upper_net_sum_not_portfolio: float
    ambiguous_trades: int
    certain_settlements: int
    ambiguous_settlements: int
    qualified: bool = False


def _checked_evidence(evidence: dict, *, boundary: pd.Timestamp, symbol: str, expected_plan_sha256: str):
    """Validate the whole frozen source timeline, even if no short was filled."""
    if not isinstance(evidence, dict) or set(evidence) != {"plan", "plan_sha256", "events"}:
        raise ValueError("funding requires a source-pinned verified evidence envelope")
    plan, events = evidence["plan"], evidence["events"]
    if not isinstance(plan, dict) or not isinstance(events, list):
        raise ValueError("invalid funding evidence types")
    crash_funding._validate_plan(plan, symbol, boundary.date())
    if (
        not isinstance(expected_plan_sha256, str)
        or len(expected_plan_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_plan_sha256)
        or evidence["plan_sha256"] != expected_plan_sha256
        or crash_funding._digest(plan) != expected_plan_sha256
    ):
        raise ValueError("funding source differs from the frozen research-plan digest")
    if len(events) != plan["event_count"] or crash_funding._digest(events) != plan["events_sha256"]:
        raise ValueError("funding event count/hash differs from the pinned source")
    start_ms = int(pd.Timestamp(plan["start"], tz="UTC").value // 1_000_000)
    end_ms = int(boundary.value // 1_000_000) - 1
    times = []
    for event in events:
        if not isinstance(event, dict) or set(event) != {"fundingTime", "fundingRate", "markPrice"}:
            raise ValueError("invalid funding event fields")
        normalized = crash_funding._event({**event, "symbol": symbol}, symbol, start_ms, end_ms)
        if normalized != event:
            raise ValueError("noncanonical funding settlement")
        times.append(event["fundingTime"])
    if times != sorted(set(times)):
        raise ValueError("duplicate or unordered funding settlement")
    crash_funding._check_coverage(times, plan["start"], plan["holdout_start"])
    return plan, events, times


def short_funding_bounds(
    replay: ShortReplay,
    costs: RoundTripCosts,
    evidence: dict,
    *,
    boundary: pd.Timestamp,
    expected_plan_sha256: str,
    symbol: str = "BTCUSDT",
) -> FundingReport:
    """Substitute conservative actual-rate bounds for modeled reserve costs.

    Positive Binance USD-M funding rates pay a short; negative rates
    charge it. Settlement ownership around minute fills is uncertain.
    The lower bound books possible payments and no uncertain receipts.
    Gross/fees/spread/slippage remain those of the existing frozen replay.

    Event hashes are a tamper check, NOT proof that the raw exchange
    data were authenticated: call crash_funding.load_verified first.
    """
    boundary = pd.Timestamp(boundary)
    if boundary.tzinfo is None or boundary.utcoffset() != timedelta(0) or boundary != boundary.floor("D"):
        raise ValueError("funding requires an exact UTC-midnight protected boundary")
    if not isinstance(replay, ShortReplay) or replay.halted or replay.unresolved_after_entry:
        raise ValueError("funding cannot value unresolved replay inventory")
    if not isinstance(costs, RoundTripCosts):
        raise ValueError("invalid cost assumptions")
    costs.total_bps  # validates estimates including the frozen funding reserve
    plan, events, times = _checked_evidence(
        evidence, boundary=boundary, symbol=symbol, expected_plan_sha256=expected_plan_sha256
    )
    start_ns = int(pd.Timestamp(plan["start"], tz="UTC").value)
    previous_exit = None
    rows = []
    for trade in replay.trades:
        _reconcile(trade, costs)
        start, end = trade.entry_minute_start_ns, trade.exit_bar_end_ns
        if (
            type(start) is not int
            or type(end) is not int
            or not start_ns <= start < end < int(boundary.value)
            or (previous_exit is not None and start < previous_exit)
        ):
            raise ValueError("short outside audited coverage, unordered, or overlapping")
        previous_exit = end
        entry = pd.Timestamp(start, unit="ns", tz="UTC")
        exit_ = pd.Timestamp(end, unit="ns", tz="UTC")
        earliest = int((entry - ENTRY_UNCERTAINTY).value // 1_000_000)
        latest = int((exit_ + ENTRY_UNCERTAINTY).value // 1_000_000)
        lo, hi = bisect_left(times, earliest), bisect_right(times, latest)
        result = funding_bounds(-1, trade.entry_reference, entry, exit_, events[lo:hi])
        no_reserve = trade.net_return + trade.funding_reserve_cost
        low = no_reserve + result["funding_return"]
        high = no_reserve + result["funding_return_upper"]
        if not all(isfinite(x) for x in (no_reserve, low, high)) or low > high + 1e-12:
            raise ValueError("invalid pessimistic funding bounds")
        rows.append(
            FundedShort(
                entry_minute_start_ns=start,
                exit_bar_end_ns=end,
                original_net_return=trade.net_return,
                no_reserve_net_return=no_reserve,
                funding_lower_return=result["funding_return"],
                funding_upper_return=result["funding_return_upper"],
                net_lower_return=low,
                net_upper_return=high,
                certain_settlements=result["funding_events"],
                ambiguous_settlements=result["funding_ambiguous_events"],
            )
        )
    return FundingReport(
        model=MODEL,
        source_plan_sha256=expected_plan_sha256,
        source_events_sha256=plan["events_sha256"],
        boundary=boundary.isoformat(),
        trades=tuple(rows),
        lower_net_sum_not_portfolio=sum(row.net_lower_return for row in rows),
        upper_net_sum_not_portfolio=sum(row.net_upper_return for row in rows),
        ambiguous_trades=sum(row.ambiguous_settlements > 0 for row in rows),
        certain_settlements=sum(row.certain_settlements for row in rows),
        ambiguous_settlements=sum(row.ambiguous_settlements for row in rows),
    )
