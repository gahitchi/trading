"""Conservative minute-bar short replay, for registered research only.

This is NOT an execution simulator: next-bar opening references, pessimistic
intra-candle ordering and fixed adverse costs cannot model the order book,
partial fills, liquidation, borrow, market impact or subsecond latency.
The protected holdout is rejected via the historical signal screener.
No broker access, no network I/O, no real or paper orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import pandas as pd

from lab.bearish_ensemble import MINUTE_NS, EnsembleSettings, MinuteBar, RoundTripCosts
from lab.bearish_ensemble_screen import SignalCensus, screen_research_frame
from lab.crash_execution import fill_accounting


@dataclass(frozen=True)
class ReplayAssumptions:
    """Completed 1-minute bars, no fake subminute fill or queue precision."""

    entry_delay_full_minutes: int = 1
    max_hold_minutes: int = 15

    def __post_init__(self) -> None:
        if (
            type(self.entry_delay_full_minutes) is not int
            or type(self.max_hold_minutes) is not int
            or self.entry_delay_full_minutes < 0
            or not 1 <= self.max_hold_minutes <= 1_440
        ):
            raise ValueError("invalid bar-delay or holding-period assumptions")


@dataclass(frozen=True)
class ShortTrade:
    signal_end_ns: int
    entry_minute_start_ns: int
    exit_bar_end_ns: int
    setup: str
    exit_reason: str
    entry_reference: float
    exit_reference: float
    entry_fill: float
    exit_fill: float
    stop_reference: float
    target_reference: float
    gross_return: float
    net_return: float
    fee_cost: float
    slippage_cost: float
    spread_cost: float
    funding_reserve_cost: float


@dataclass(frozen=True)
class ShortReplay:
    census: SignalCensus
    trades: tuple[ShortTrade, ...]
    skipped_before_entry: int
    ignored_while_open: int
    unresolved_after_entry: int
    halted: bool
    assumptions: ReplayAssumptions

    @property
    def total_net_trade_return(self) -> float:
        """Arithmetic trade return sum; not portfolio equity or compounded P&L."""
        return sum(t.net_return for t in self.trades)


def _bar(frame: pd.DataFrame, i: int) -> MinuteBar | None:
    if bool(frame["oi_invalid"].iat[i]):
        return None
    row = frame.iloc[i]
    try:
        bar = MinuteBar(
            int(frame.index[i].value),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["quote_volume"]),
            float(row["taker_buy_quote"]),
        )
        bar.validate()
    except (TypeError, ValueError, OverflowError):
        return None
    return bar


def _fill(short_entry: float, exit_ref: float, costs: RoundTripCosts) -> dict:
    # Sell at modeled bid, cover at modeled ask. Slippage and fees are
    # charged on executed notional by the project's existing cost kernel.
    half_spread = costs.spread_bps / 20_000
    bid = short_entry * (1 - half_spread)
    ask = exit_ref * (1 + half_spread)
    if bid <= 0:
        raise ValueError("spread makes entry bid nonpositive")
    result = fill_accounting(-1, bid, ask, costs.fee_per_side_bps, costs.slippage_per_side_bps)
    # The shared fill kernel uses its entry reference as denominator
    # (bid here). Normalize every term to the ORIGINAL candle-open
    # reference to keep a single, checkable economic identity.
    scale = bid / short_entry
    gross = (short_entry - exit_ref) / short_entry
    spread = (short_entry - bid + ask - exit_ref) / short_entry
    slippage = result["slippage_cost"] * scale
    fee = result["fee_cost"] * scale
    reserve = costs.adverse_funding_bps / 10_000
    net = gross - spread - slippage - fee - reserve
    return {
        "entry_fill": result["entry_fill"],
        "exit_fill": result["exit_fill"],
        "gross_return": gross,
        "net_return": net,
        "fee_cost": fee,
        "slippage_cost": slippage,
        "spread_cost": spread,
        "funding_reserve_cost": reserve,
    }


def replay_research_frame(
    frame: pd.DataFrame,
    *,
    boundary: pd.Timestamp,
    costs: RoundTripCosts,
    signal_settings: EnsembleSettings | None = None,
    assumptions: ReplayAssumptions | None = None,
) -> ShortReplay:
    """Replay short-only signals against conservatively ordered OHLC.

    Research only: this deliberately does NOT register an experiment or
    open parquet files. The caller must pre-register and use the guarded
    archived loader before passing a frame. No realized funding ledger or
    daily marked-equity accounting is produced.
    """
    assumptions = assumptions or ReplayAssumptions()
    census = screen_research_frame(frame, boundary=boundary, costs=costs, settings=signal_settings)
    if not census.candidate_times_ns:
        return ShortReplay(census, (), 0, 0, 0, False, assumptions)

    offsets = frame.index.get_indexer(pd.to_datetime(census.candidate_times_ns, unit="ns", utc=True))
    if (offsets < 0).any():
        raise ValueError("missing signal timestamp")
    trades: list[ShortTrade] = []
    skipped = ignored = unresolved = 0
    open_until_index = -1
    halted = False
    n = len(frame)
    delay = assumptions.entry_delay_full_minutes
    for position, signal_i in enumerate(offsets):
        if signal_i <= open_until_index:
            ignored += 1
            continue
        entry_i = int(signal_i) + delay + 1
        if entry_i >= n:
            skipped += 1
            continue
        signal_ns = int(census.candidate_times_ns[position])
        # Entry delay is one or more WHOLE minutes after the signal,
        # rather than an invented fill at 50/150/300 ms.
        if any(
            frame.index[i].value != signal_ns + (i - signal_i) * MINUTE_NS or _bar(frame, i) is None
            for i in range(int(signal_i) + 1, entry_i + 1)
        ):
            skipped += 1
            continue
        first = _bar(frame, entry_i)
        assert first is not None  # guarded above; no state mutation
        entry_ref = first.open
        stop_bps = census.candidate_stops_bps[position]
        target_bps = census.candidate_targets_bps[position]
        if not all(isfinite(v) and v > 0 for v in (stop_bps, target_bps)):
            skipped += 1
            continue
        stop = entry_ref * (1 + stop_bps / 10_000)
        target = entry_ref * (1 - target_bps / 10_000)
        if target <= 0:
            skipped += 1
            continue
        completed = False
        for j in range(entry_i, min(n, entry_i + assumptions.max_hold_minutes)):
            candle = _bar(frame, j)
            if candle is None or candle.end_ns != first.end_ns + (j - entry_i) * MINUTE_NS:
                # There is an unobserved path while inventory is open.
                # Fail closed: do not fabricate an exit or keep trading.
                unresolved += 1
                halted = True
                break
            if candle.high >= stop:
                reason = "stop"
                exit_ref = max(stop, candle.open)  # gap through stop worsens fill
            elif candle.low <= target:
                reason = "target"
                exit_ref = target  # no optimistic gain from a favorable gap
            elif j == entry_i + assumptions.max_hold_minutes - 1:
                reason = "time"
                exit_ref = candle.close
            else:
                continue
            executed = _fill(entry_ref, exit_ref, costs)
            trades.append(
                ShortTrade(
                    signal_end_ns=signal_ns,
                    entry_minute_start_ns=int(first.end_ns - MINUTE_NS),
                    exit_bar_end_ns=candle.end_ns,
                    setup=census.candidate_setups[position],
                    exit_reason=reason,
                    entry_reference=entry_ref,
                    exit_reference=exit_ref,
                    entry_fill=executed["entry_fill"],
                    exit_fill=executed["exit_fill"],
                    stop_reference=stop,
                    target_reference=target,
                    gross_return=executed["gross_return"],
                    net_return=executed["net_return"],
                    fee_cost=executed["fee_cost"],
                    slippage_cost=executed["slippage_cost"],
                    spread_cost=executed["spread_cost"],
                    funding_reserve_cost=executed["funding_reserve_cost"],
                )
            )
            open_until_index = j
            completed = True
            break
        if halted:
            break
        if not completed:
            unresolved += 1
            halted = True
            break
    return ShortReplay(census, tuple(trades), skipped, ignored, unresolved, halted, assumptions)
