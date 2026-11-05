"""Funding-adjusted exploratory crash trade ledger, using audited Binance events.

No source ingestion and no strategy selection occurs in this module.
Every included settlement is valued at its exchange mark; ambiguous
within-bar ownership or incomplete coverage blocks the whole evaluation.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import replace

import pandas as pd

from lab.crash_funding import MAX_SETTLEMENT_OFFSET_MS, settlement_return

MODEL = "audited-fixed-base-mark-settlement-minute-exit-v1"
EXIT_UNCERTAINTY = pd.Timedelta(minutes=1)


def funded_trades(trades: pd.DataFrame, side: int, evidence: dict) -> pd.DataFrame:
    """Adjust executed-notional net return with real short/long funding.

    Entry is the open of the recorded entry bar. The study's exit
    timestamp is the END of the exit minute; a stop or target might
    have filled anywhere within that minute. Never decide who owned
    a settlement inside the exit-minute uncertainty interval.
    """
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
    output["funding_return"] = pd.Series(0.0, index=output.index, dtype=float)
    output["funding_events"] = pd.Series(0, index=output.index, dtype=int)

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

        # The exit bar is only a minute OHLC record, not a timestamped
        # execution. The settlement could precede or follow its stop
        # or target fill, so fail rather than assign favourable funding.
        # The time-based exit at bar close is conservatively subject
        # to this same ambiguity rule.
        lo = bisect_left(times, int(entry.value // 1_000_000))
        hi = bisect_right(times, int(exit_.value // 1_000_000))
        # Include neighbouring settlement events for boundary checks
        # even when no settlement actually falls inside the trade.
        nearby = events[max(lo - 1, 0):min(hi + 1, len(events))]
        uncertainty_start = exit_ - EXIT_UNCERTAINTY - pd.Timedelta(
            milliseconds=MAX_SETTLEMENT_OFFSET_MS
        )
        uncertainty_end = exit_ + pd.Timedelta(milliseconds=MAX_SETTLEMENT_OFFSET_MS)
        for event in nearby:
            ts = pd.Timestamp(event["fundingTime"], unit="ms", tz="UTC")
            if uncertainty_start <= ts <= uncertainty_end:
                raise ValueError(
                    "ambiguous funding settlement inside unknown exit-minute execution interval"
                )

        result = settlement_return(
            side, float(trade["entry_reference"]), entry, exit_, nearby,
        )
        output.at[idx, "funding_return"] = result["funding_return"]
        output.at[idx, "funding_events"] = result["settlements"]
        output.at[idx, "net"] = float(trade["net"]) + result["funding_return"]
    return output


def funded_run(run, side: int, evidence: dict):
    """Funding-adjust strategy and daily baseline, leaving trade selection intact."""
    return replace(
        run,
        trades={name: funded_trades(t, side, evidence) for name, t in run.trades.items()},
        baseline={name: funded_trades(t, side, evidence) for name, t in run.baseline.items()},
    )


def funding_totals(trades_by_symbol: dict[str, pd.DataFrame]) -> dict:
    """Summarize actual event incidence separately from price/fee P&L."""
    frames = list(trades_by_symbol.values())
    return {
        "trades": int(sum(len(f) for f in frames)),
        "funded_settlements": int(sum(f["funding_events"].sum() for f in frames)),
        "funding_return_sum_bps": float(sum(f["funding_return"].sum() for f in frames) * 10000),
        "funding_receiving_trades": int(sum((f["funding_return"] > 0).sum() for f in frames)),
        "funding_paying_trades": int(sum((f["funding_return"] < 0).sum() for f in frames)),
    }
