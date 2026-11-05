"""Audited pre-holdout funding P&L with conservative minute-fill uncertainty.

The actual funding rate, mark and timestamp come from verified Binance
settlement events. An OHLC bar does not identify the exact exit execution
time. For any settlement whose ownership is uncertain, preserve the full
lower/upper possible funding P&L interval and use the LOWER endpoint
for the research result. Never omit a trade or claim an uncertain
settlement as an observed payment.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import replace
from decimal import Decimal

import pandas as pd

from lab.crash_funding import MAX_SETTLEMENT_OFFSET_MS, _decimal

MODEL = "audited-fixed-base-mark-minute-fill-funding-bounds-v2"
ENTRY_UNCERTAINTY = pd.Timedelta(milliseconds=MAX_SETTLEMENT_OFFSET_MS)
EXIT_UNCERTAINTY = pd.Timedelta(minutes=1) + ENTRY_UNCERTAINTY


def funding_bounds(
    side: int, entry_reference: float, entry_time: pd.Timestamp,
    exit_time: pd.Timestamp, events: list[dict],
) -> dict:
    """Funded cashflows for all possible ownership of uncertain settlements.

    Trade entry is modelled at the open of a minute. Ownership within
    +/- 30 s of that open is conservatively uncertain, as are funding
    events in the exit minute or within 30 s after the exit timestamp.
    Guaranteed ownership requires a settlement strictly after the
    entry uncertainty window and before the earliest possible exit.

    Bounds are not predicted funding, strategy choices, or new fills.
    All values are returns on the same initial reference notional.
    """
    if type(side) is not int or side not in (-1, 1):
        raise ValueError("invalid perpetual position side")
    if (
        isinstance(entry_reference, bool)
        or not isinstance(entry_reference, (float, int))
        or not math.isfinite(entry_reference)
        or entry_reference <= 0
    ):
        raise ValueError("invalid position entry reference")
    entry = pd.Timestamp(entry_time)
    exit_ = pd.Timestamp(exit_time)
    if (
        entry.tzinfo is None or exit_.tzinfo is None
        or entry.utcoffset() != pd.Timedelta(0)
        or exit_.utcoffset() != pd.Timedelta(0)
        or entry >= exit_
    ):
        raise ValueError("funding position times must be ordered UTC instants")

    certain = Decimal("0")
    lower = Decimal("0")
    upper = Decimal("0")
    certain_events = ambiguous_events = 0
    previous = None
    for event in events:
        raw = event["fundingTime"]
        if type(raw) is not int:
            raise ValueError("invalid settlement timestamp")
        when = pd.Timestamp(raw, unit="ms", tz="UTC")
        if previous is not None and when <= previous:
            raise ValueError("duplicate or unordered settlement timestamps")
        previous = when
        rate = Decimal(_decimal(event["fundingRate"]))
        if abs(rate) >= 1:
            raise ValueError("invalid settlement funding magnitude")
        mark = Decimal(_decimal(event["markPrice"], positive=True))
        delta = Decimal(-side) * rate * mark / Decimal(str(entry_reference))
        if entry + ENTRY_UNCERTAINTY < when < exit_ - EXIT_UNCERTAINTY:
            certain += delta
            lower += delta
            upper += delta
            certain_events += 1
        elif entry - ENTRY_UNCERTAINTY <= when <= exit_ + ENTRY_UNCERTAINTY:
            # Unknown ownership. The unfavorable result is charged if
            # it is a cost, while a hypothetical receipt is never booked
            # as certain in the lower (reported) P&L.
            lower += min(Decimal("0"), delta)
            upper += max(Decimal("0"), delta)
            ambiguous_events += 1
    return {
        "funding_return": float(lower),
        "funding_return_upper": float(upper),
        "funding_return_certain": float(certain),
        "funding_events": certain_events,
        "funding_ambiguous_events": ambiguous_events,
        "funding_bound_width": float(upper - lower),
    }


def funded_trades(trades: pd.DataFrame, side: int, evidence: dict) -> pd.DataFrame:
    """Add settlement evidence to all original trades, with no trade exclusions."""
    plan, events = evidence["plan"], evidence["events"]
    if not events:
        raise ValueError("funding ledger requires audited settlement events")
    times = [int(event["fundingTime"]) for event in events]
    if times != sorted(set(times)):
        raise ValueError("funding ledger requires ordered unique settlement events")
    first = pd.Timestamp(plan["start"], tz="UTC")
    cutoff = pd.Timestamp(plan["holdout_start"], tz="UTC")
    required = {"entry", "exit", "entry_reference", "net", "reason"}
    if not required <= set(trades):
        raise ValueError("funding ledger lacks original trade-time or economic fields")
    output = trades.copy()
    for field, default in (
        ("funding_return", 0.0), ("funding_return_upper", 0.0),
        ("funding_return_certain", 0.0), ("funding_bound_width", 0.0),
        ("funding_events", 0), ("funding_ambiguous_events", 0),
        ("net_upper", 0.0),
    ):
        output[field] = pd.Series(default, index=output.index)

    for idx, trade in output.iterrows():
        entry = pd.Timestamp(trade["entry"])
        exit_ = pd.Timestamp(trade["exit"])
        if (
            entry.tzinfo is None or exit_.tzinfo is None
            or entry.utcoffset() != pd.Timedelta(0)
            or exit_.utcoffset() != pd.Timedelta(0)
            or not (first <= entry < exit_ < cutoff)
        ):
            raise ValueError("funding trade interval is outside audited pre-holdout research coverage")

        # Include all potential settlements in the conservative execution
        # uncertainty interval, including events just before entry and
        # just after exit. All other events have zero ownership probability.
        from_ms = int((entry - ENTRY_UNCERTAINTY).value // 1_000_000)
        to_ms = int((exit_ + ENTRY_UNCERTAINTY).value // 1_000_000)
        lo = bisect_left(times, from_ms)
        hi = bisect_right(times, to_ms)
        result = funding_bounds(
            side, float(trade["entry_reference"]), entry, exit_, events[lo:hi],
        )
        for field, val in result.items():
            output.at[idx, field] = val
        original = float(trade["net"])
        output.at[idx, "net"] = original + result["funding_return"]
        output.at[idx, "net_upper"] = original + result["funding_return_upper"]
    return output


def funded_run(run, side: int, evidence: dict):
    """Account for all strategy and baseline trades without selecting on P&L."""
    return replace(
        run,
        trades={name: funded_trades(t, side, evidence) for name, t in run.trades.items()},
        baseline={name: funded_trades(t, side, evidence) for name, t in run.baseline.items()},
    )


def funding_totals(trades_by_symbol: dict[str, pd.DataFrame]) -> dict:
    """Sum pessimistic and optimistic funding bounds, with ambiguity counts."""
    frames = list(trades_by_symbol.values())
    return {
        "trades": int(sum(len(f) for f in frames)),
        "funded_settlements": int(sum(f["funding_events"].sum() for f in frames)),
        "ambiguous_settlements": int(sum(f["funding_ambiguous_events"].sum() for f in frames)),
        "trades_with_ambiguous_funding": int(sum((f["funding_ambiguous_events"] > 0).sum() for f in frames)),
        "funding_return_sum_bps": float(sum(f["funding_return"].sum() for f in frames) * 1e4),
        "funding_return_upper_sum_bps": float(sum(f["funding_return_upper"].sum() for f in frames) * 1e4),
        "uncertainty_width_sum_bps": float(sum(f["funding_bound_width"].sum() for f in frames) * 1e4),
        "funding_receiving_trades": int(sum((f["funding_return"] > 0).sum() for f in frames)),
        "funding_paying_trades": int(sum((f["funding_return"] < 0).sum() for f in frames)),
    }
