"""Conservative daily virtual equity from a completed offline hybrid replay.

Reference outcomes only. Uses replay-proposed fixed notionals, NOT compounding
or authenticated execution. Rejects incomplete days and unresolved inventory.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isclose, isfinite

import pandas as pd

from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_replay import ShortTrade
from lab.bearish_equity import _closeout, _reconcile
from lab.bearish_hybrid import MINUTE_NS, Costs, Risk, VirtualAccount, paper_size
from lab.bearish_hybrid_replay import DAY_NS, HybridObservation, HybridReplay


@dataclass(frozen=True)
class VirtualDay:
    day: str
    start_equity: float
    end_equity: float
    net_pnl: float
    daily_return: float
    realized_cash: float
    unrealized_after_closeout: float
    entries: int
    exits: int
    open_position: bool


@dataclass(frozen=True)
class VirtualLedger:
    """All values hypothetical USDT; no live cash or verified profitability."""

    days: tuple[VirtualDay, ...]
    initial_equity: float
    ending_equity: float
    realized_cash: float
    total_return: float
    max_daily_drawdown: float
    completed_trades: int
    positive_days: int
    negative_days: int
    flat_days: int
    funded_with_real_cash: bool = False
    position_sizing_compounds: bool = False


def daily_hybrid_virtual_ledger(
    observations: Sequence[HybridObservation],
    replay: HybridReplay,
    *,
    costs: Costs,
    account: VirtualAccount,
    boundary_ns: int,
    risk: Risk | None = None,
) -> VirtualLedger:
    """Mark complete UTC days and settle *modeled* trade returns at exit.

    Requires the same single-symbol observations that produced the replay;
    the caller must additionally establish archive integrity externally.
    Notionals are frozen by replay and are never recalculated from profits.
    No unresolved active inventory is permitted; no liquidation modeled.
    """
    account.check()
    if (
        not isfinite(account.equity)
        or account.equity <= 0
        or not isclose(account.equity, account.day_start_equity, rel_tol=0, abs_tol=1e-9)
        or account.current_gross_notional != 0
        or account.open_positions != 0
        or account.day_pnl != 0
    ):
        raise ValueError("ledger needs a funded, initially flat virtual snapshot with no prior daily P&L")
    if type(boundary_ns) is not int or boundary_ns <= 0 or boundary_ns % DAY_NS:
        raise ValueError("protected boundary must be an aligned positive UTC midnight")
    if not isinstance(replay, HybridReplay) or replay.halted or replay.unresolved_after_entry:
        raise ValueError("unresolved replay cannot produce a daily equity claim")
    risk = risk or Risk()
    trades = replay.trades
    notionals = replay.virtual_notionals
    if len(trades) != len(notionals):
        raise ValueError("trade/notional accounting mismatch")
    roundtrip = RoundTripCosts(
        costs.fee_per_side_bps, costs.slippage_per_side_bps, costs.full_spread_bps, costs.adverse_funding_bps
    )
    if roundtrip.total_bps >= 10_000:
        raise ValueError("invalid modeled cost schedule")
    if (
        not observations
        or (observations[0].bar.end_ns - MINUTE_NS) % DAY_NS
        or observations[-1].bar.end_ns % DAY_NS
        or len(observations) % 1_440
    ):
        raise ValueError("daily equity needs exact complete UTC days starting at 00:01 and ending at 00:00")

    # Validate every bar before touching a trade. Even a flat day's prices
    # cannot be fabricated or interpolated to make a daily return.
    for i, record in enumerate(observations):
        if not isinstance(record, HybridObservation) or record.source_complete is not True:
            raise ValueError("untrusted or incomplete daily price evidence")
        record.bar.check()
        end_ns = record.bar.end_ns
        if end_ns >= boundary_ns or (i and end_ns - observations[i - 1].bar.end_ns != MINUTE_NS):
            raise ValueError("price gap or protected holdout in daily ledger")

    first_start = observations[0].bar.end_ns - MINUTE_NS
    last_end = observations[-1].bar.end_ns
    prev_exit_ns = first_start
    entries: list[int] = []
    exits: list[int] = []
    for trade, notional in zip(trades, notionals, strict=True):
        if not isinstance(trade, ShortTrade):
            raise ValueError("invalid replay trade record")
        _reconcile(trade, roundtrip)
        if not isfinite(notional) or notional <= 0:
            raise ValueError("invalid modeled virtual notional")
        start, end = trade.entry_minute_start_ns, trade.exit_bar_end_ns
        if (
            type(start) is not int
            or type(end) is not int
            or start < first_start
            or start < prev_exit_ns
            or start >= end
            or end > last_end
            or (start - first_start) % MINUTE_NS
            or (end - first_start) % MINUTE_NS
        ):
            raise ValueError("invalid or overlapping trade timing")
        ii = (start - first_start) // MINUTE_NS
        jj = (end - first_start) // MINUTE_NS - 1
        if not isclose(observations[ii].bar.open, trade.entry_reference, rel_tol=1e-10):
            raise ValueError("entry disagrees with archived opening price")
        if not trade.target_reference < trade.entry_reference < trade.stop_reference:
            raise ValueError("invalid short stop/target geometry")
        # Replay notionals are caller-controlled inputs, so independently
        # enforce the same frozen-account risk envelope before any P&L.
        stop_bps = (trade.stop_reference / trade.entry_reference - 1) * 10_000
        allowed = paper_size(account, risk, stop_bps, costs.round_trip_bps)
        if notional > allowed + 1e-8:
            raise ValueError("virtual notional exceeds replay account risk cap")
        for k in range(ii, jj + 1):
            candle = observations[k].bar
            touched_stop = candle.high >= trade.stop_reference
            touched_target = candle.low <= trade.target_reference
            is_exit = k == jj
            if not is_exit and (touched_stop or touched_target):
                raise ValueError("replay ignored earlier stop/target breach")
            if is_exit:
                if touched_stop:
                    expected_reason = "stop"
                    expected_price = max(candle.open, trade.stop_reference)
                elif touched_target:
                    expected_reason = "target"
                    expected_price = trade.target_reference
                elif k - ii + 1 == replay.assumptions.max_hold_minutes:
                    expected_reason = "time"
                    expected_price = candle.close
                else:
                    raise ValueError("trade exits without an OHLC trigger")
                if trade.exit_reason != expected_reason or not isclose(
                    trade.exit_reference, expected_price, rel_tol=1e-10
                ):
                    raise ValueError("modeled exit contradicts stop-first candle path")
        if jj - ii + 1 > replay.assumptions.max_hold_minutes:
            raise ValueError("trade exceeds allowed holding window")
        entries.append(ii)
        exits.append(jj)
        prev_exit_ns = end

    cash = account.equity
    previous_equity = cash
    peak = cash
    drawdown = 0.0
    ptr = 0
    active: int | None = None
    day_entries = day_exits = 0
    results: list[VirtualDay] = []
    fee_rate = costs.fee_per_side_bps / 10_000
    for i, observation in enumerate(observations):
        bar = observation.bar
        if active is None and ptr < len(trades) and i == entries[ptr]:
            active = ptr
            day_entries += 1
        if active is not None:
            trade, notional = trades[active], notionals[active]
            qty = notional / trade.entry_reference
            entry_fee = qty * trade.entry_fill * fee_rate
            worst_cover = _closeout(bar.high, roundtrip)
            worst_fee = entry_fee + qty * worst_cover * fee_rate
            worst_equity = (
                cash + qty * (trade.entry_fill - worst_cover) - worst_fee - notional * trade.funding_reserve_cost
            )
            if worst_equity <= 0:
                raise ValueError("unresolved virtual insolvency under intrabar adverse-price stress")
            if i == exits[active]:
                cash += notional * trade.net_return
                if not isfinite(cash) or cash <= 0:
                    raise ValueError("modeled virtual equity depleted; liquidation unavailable")
                day_exits += 1
                ptr += 1
                active = None
        if bar.end_ns % DAY_NS == 0:
            equity = cash
            if active is not None:
                trade, notional = trades[active], notionals[active]
                qty = notional / trade.entry_reference
                cover = _closeout(bar.close, roundtrip)
                entry_fee = qty * trade.entry_fill * fee_rate
                exit_fee = qty * cover * fee_rate
                equity += (
                    qty * (trade.entry_fill - cover) - entry_fee - exit_fee - notional * trade.funding_reserve_cost
                )
            if not isfinite(equity) or equity <= 0:
                raise ValueError("unqualified insolvent daily virtual equity")
            net = equity - previous_equity
            day = pd.Timestamp(bar.end_ns - MINUTE_NS, unit="ns", tz="UTC").date().isoformat()
            results.append(
                VirtualDay(
                    day,
                    previous_equity,
                    equity,
                    net,
                    net / previous_equity,
                    cash,
                    equity - cash,
                    day_entries,
                    day_exits,
                    active is not None,
                )
            )
            previous_equity = equity
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
            day_entries = day_exits = 0
    if active is not None or ptr != len(trades):
        raise ValueError("open or unaccounted terminal virtual inventory")
    return VirtualLedger(
        days=tuple(results),
        initial_equity=account.equity,
        ending_equity=cash,
        realized_cash=cash,
        total_return=cash / account.equity - 1,
        max_daily_drawdown=drawdown,
        completed_trades=len(trades),
        positive_days=sum(d.net_pnl > 0 for d in results),
        negative_days=sum(d.net_pnl < 0 for d in results),
        flat_days=sum(d.net_pnl == 0 for d in results),
    )
