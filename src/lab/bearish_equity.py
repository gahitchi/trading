"""Research-only marked-equity ledger for modeled, single-symbol short trades.

Uses completed UTC minute candles and already constructed ShortReplay trades.
Fixed fraction of CURRENT realized equity per short; zero external flows.
Daily marks use conservative hypothetical buy-to-cover prices and both fees.
No actual funding, broker fills, partial fills, liquidation or qualified edge.
No market-data or trial-file access. Never authorize an order from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import isclose, isfinite

import numpy as np
import pandas as pd

from lab.bearish_ensemble import MINUTE_NS, RoundTripCosts
from lab.bearish_ensemble_replay import ShortReplay, ShortTrade

MODEL = "short-minute-daily-conservative-equity-v1"


@dataclass(frozen=True)
class EquityAssumptions:
    initial_equity: float = 10_000.0
    notional_fraction: float = 0.25
    minimum_equity_fraction: float = 0.10

    def __post_init__(self) -> None:
        values = (self.initial_equity, self.notional_fraction, self.minimum_equity_fraction)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) for v in values):
            raise ValueError("equity assumptions must be finite real numbers")
        if self.initial_equity <= 0 or not 0 < self.notional_fraction <= 1:
            raise ValueError("initial equity must be positive and notional fraction in (0, 1]")
        if not 0 <= self.minimum_equity_fraction < 1:
            raise ValueError("minimum equity fraction must lie in [0, 1)")


@dataclass(frozen=True)
class DailyEquity:
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
class EquityReport:
    model: str
    assumptions: EquityAssumptions
    days: tuple[DailyEquity, ...]
    completed_trades: int
    start_equity: float
    end_equity: float
    total_return: float
    max_drawdown: float
    positive_days: int
    negative_days: int
    flat_days: int
    qualified: bool = False


def _closeout(reference: float, costs: RoundTripCosts) -> float:
    """Hypothetical full close-out at the observed close, including adverse costs."""
    spread = costs.spread_bps / 20_000
    slip = costs.slippage_per_side_bps / 10_000
    return reference * (1 + spread) * (1 + slip)


def _reconcile(trade: ShortTrade, costs: RoundTripCosts) -> None:
    """Reject modified trade economics before creating a portfolio equity claim."""
    nums = (
        trade.entry_reference,
        trade.exit_reference,
        trade.entry_fill,
        trade.exit_fill,
        trade.stop_reference,
        trade.target_reference,
        trade.gross_return,
        trade.net_return,
        trade.fee_cost,
        trade.slippage_cost,
        trade.spread_cost,
        trade.funding_reserve_cost,
    )
    if not all(isfinite(x) for x in nums) or min(nums[:6]) <= 0:
        raise ValueError("invalid modeled trade price or cost evidence")
    fee = costs.fee_per_side_bps / 10_000
    slip = costs.slippage_per_side_bps / 10_000
    half_spread = costs.spread_bps / 20_000
    entry = trade.entry_reference * (1 - half_spread) * (1 - slip)
    exit_ = trade.exit_reference * (1 + half_spread) * (1 + slip)
    expected_fee = fee * (entry + exit_) / trade.entry_reference
    bid = trade.entry_reference * (1 - half_spread)
    ask = trade.exit_reference * (1 + half_spread)
    expected_spread = (trade.entry_reference - bid + ask - trade.exit_reference) / trade.entry_reference
    expected_slippage = (bid - entry + exit_ - ask) / trade.entry_reference
    gross = (trade.entry_reference - trade.exit_reference) / trade.entry_reference
    net = (entry - exit_) / trade.entry_reference - expected_fee - costs.adverse_funding_bps / 10_000
    checks = (
        (trade.entry_fill, entry),
        (trade.exit_fill, exit_),
        (trade.fee_cost, expected_fee),
        (trade.spread_cost, expected_spread),
        (trade.slippage_cost, expected_slippage),
        (trade.gross_return, gross),
        (trade.net_return, net),
        (trade.funding_reserve_cost, costs.adverse_funding_bps / 10_000),
        (
            trade.net_return,
            trade.gross_return - trade.fee_cost - trade.slippage_cost - trade.spread_cost - trade.funding_reserve_cost,
        ),
    )
    if any(not isclose(a, b, rel_tol=1e-9, abs_tol=1e-11) for a, b in checks):
        raise ValueError("modeled trade fills/costs fail cash conservation")


def daily_short_equity(
    frame: pd.DataFrame,
    replay: ShortReplay,
    costs: RoundTripCosts,
    assumptions: EquityAssumptions | None = None,
) -> EquityReport:
    """Full UTC-day reference equity, with flat days and MTM open shorts.

    Requires exactly complete UTC days: a first 00:01 candle-end stamp,
    a final 00:00 UTC end stamp, and every intervening minute observed.
    No interpolation, no terminal position, no incomplete-day selection.
    A 10%-of-notional worst-high equity floor is a research veto, not an
    exchange liquidation engine; no nonpositive equity is permitted.
    """
    a = assumptions or EquityAssumptions()
    if not isinstance(replay, ShortReplay) or replay.halted or replay.unresolved_after_entry:
        raise ValueError("cannot mark an unresolved short replay")
    if not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("equity requires a UTC candle-end-indexed DataFrame")
    idx = frame.index
    if idx.tz is None or str(idx.tz) != "UTC" or not idx.is_monotonic_increasing or not idx.is_unique:
        raise ValueError("equity requires unique increasing UTC candle-end stamps")
    if len(idx) < 1440 or (idx.asi8 <= 0).any() or (idx.asi8 % MINUTE_NS).any():
        raise ValueError("equity requires complete positive UTC minute timestamps")
    if idx[0] != idx[0].floor("D") + pd.Timedelta(minutes=1) or idx[-1] != idx[-1].floor("D"):
        raise ValueError("equity window must consist of full UTC days")
    if np.any(np.diff(idx.asi8) != MINUTE_NS):
        raise ValueError("minute-price gaps prohibit continuous equity marks")
    if not {"open", "high", "low", "close", "oi_invalid"} <= set(frame.columns):
        raise ValueError("equity requires OHLC and source-quality evidence")
    quality = frame["oi_invalid"]
    if quality.isna().any() or not pd.api.types.is_bool_dtype(quality.dtype):
        raise ValueError("invalid source quality marker")
    prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("invalid candle price prevents continuous mark")
    if np.any(prices[:, 1] < np.maximum(prices[:, 0], prices[:, 3])) or np.any(
        prices[:, 2] > np.minimum(prices[:, 0], prices[:, 3])
    ):
        raise ValueError("inconsistent OHLC prevents equity marks")
    if costs.total_bps >= 10_000 or costs.fee_per_side_bps >= 10_000 or costs.slippage_per_side_bps >= 10_000:
        raise ValueError("unsupported fill costs")

    trades = replay.trades
    for trade in trades:
        if not isinstance(trade, ShortTrade):
            raise ValueError("invalid short trade record")
        _reconcile(trade, costs)
    if any(trades[i].entry_minute_start_ns < trades[i - 1].exit_bar_end_ns for i in range(1, len(trades))):
        raise ValueError("overlapping or unordered inventory")

    closed_ns = idx.asi8
    starts = [int(trade.entry_minute_start_ns) for trade in trades]
    ends = [int(trade.exit_bar_end_ns) for trade in trades]
    for trade, start, end in zip(trades, starts, ends, strict=True):
        if not start < end or start < int(closed_ns[0]) - MINUTE_NS or end > int(closed_ns[-1]):
            raise ValueError("trade occurs beyond complete source day or has invalid timing")
        if start % MINUTE_NS or end % MINUTE_NS:
            raise ValueError("trade execution outside minute boundaries")
        i = idx.get_indexer([pd.Timestamp(start + MINUTE_NS, unit="ns", tz="UTC")])[0]
        j = idx.get_indexer([pd.Timestamp(end, unit="ns", tz="UTC")])[0]
        if i < 0 or j < i:
            raise ValueError("missing entry/exit candle evidence")
        if not isclose(float(prices[i, 0]), trade.entry_reference, rel_tol=1e-10):
            raise ValueError("entry reference differs from archived candle open")
        if quality.iloc[i : j + 1].any():
            raise ValueError("unknown source quality while short inventory is open")
        if trade.exit_reason == "time" and not isclose(float(prices[j, 3]), trade.exit_reference, rel_tol=1e-10):
            raise ValueError("time exit disagrees with archived candle close")

    close_dates = idx[(idx.hour == 0) & (idx.minute == 0)]
    if not len(close_dates) or len(close_dates) * 1440 != len(idx):
        raise ValueError("incomplete UTC day in price source")
    cash = a.initial_equity
    prev_equity = cash
    peak = cash
    worst_dd = 0.0
    rows: list[DailyEquity] = []
    pointer = 0
    active: tuple[ShortTrade, float, float, float] | None = None

    for mark in close_dates:
        ns = int(mark.value)
        entries = exits = 0
        while True:
            if active is not None:
                trade, qty, base, _fee_entry = active
                if trade.exit_bar_end_ns > ns:
                    break
                cash = base + qty * trade.entry_reference * trade.net_return
                if cash <= 0:
                    raise ValueError("equity depleted by modeled short")
                exits += 1
                pointer += 1
                active = None
                continue
            if pointer >= len(trades) or starts[pointer] >= ns:
                break
            trade = trades[pointer]
            start, end = starts[pointer], ends[pointer]
            base = cash
            if base <= 0:
                raise ValueError("equity depleted; liquidation or margin model required")
            notional = base * a.notional_fraction
            qty = notional / trade.entry_reference
            fee_entry = qty * trade.entry_fill * costs.fee_per_side_bps / 10_000
            entries += 1
            i = idx.get_indexer([pd.Timestamp(start + MINUTE_NS, unit="ns", tz="UTC")])[0]
            j = idx.get_indexer([pd.Timestamp(end, unit="ns", tz="UTC")])[0]
            worst_cover = _closeout(float(prices[i : j + 1, 1].max()), costs)
            worst_fee = fee_entry + qty * worst_cover * costs.fee_per_side_bps / 10_000
            stress_equity = base + qty * (trade.entry_fill - worst_cover) - worst_fee - notional * trade.funding_reserve_cost
            if stress_equity <= notional * a.minimum_equity_fraction:
                raise ValueError("intrabar equity floor breached; liquidation not modeled")
            active = (trade, qty, base, fee_entry)
        if active is not None:
            trade, qty, base, fee_entry = active
            mark_px = float(frame.loc[mark, "close"])
            cover = _closeout(mark_px, costs)
            exit_commission = qty * cover * costs.fee_per_side_bps / 10_000
            # Never use the eventual exit duration to value an earlier day:
            # charge the complete adverse funding reserve while open.
            estimated_reserve = qty * trade.entry_reference * trade.funding_reserve_cost
            equity = base + qty * (trade.entry_fill - cover) - fee_entry - exit_commission - estimated_reserve
        else:
            equity = cash
        if equity <= 0 or not isfinite(equity):
            raise ValueError("invalid or insolvent marked account equity")
        daily_pnl = equity - prev_equity
        day_label = (mark - timedelta(minutes=1)).date().isoformat()
        rows.append(
            DailyEquity(
                day=day_label,
                start_equity=prev_equity,
                end_equity=equity,
                net_pnl=daily_pnl,
                daily_return=daily_pnl / prev_equity,
                realized_cash=cash,
                unrealized_after_closeout=equity - cash,
                entries=entries,
                exits=exits,
                open_position=active is not None,
            )
        )
        prev_equity = equity
        peak = max(peak, equity)
        worst_dd = max(worst_dd, (peak - equity) / peak)
    if active is not None or pointer != len(trades):
        raise ValueError("terminal short remains open or unaccounted")
    return EquityReport(
        model=MODEL,
        assumptions=a,
        days=tuple(rows),
        completed_trades=len(trades),
        start_equity=a.initial_equity,
        end_equity=cash,
        total_return=cash / a.initial_equity - 1,
        max_drawdown=worst_dd,
        positive_days=sum(row.net_pnl > 0 for row in rows),
        negative_days=sum(row.net_pnl < 0 for row in rows),
        flat_days=sum(row.net_pnl == 0 for row in rows),
    )
