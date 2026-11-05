"""Frozen v1 BTCUSDT failed-support-retest short candidate.

This module implements the ALREADY-DOCUMENTED hypothesis, not a tuned
variant of the losing immediate-crash strategy. It reads only bars supplied
by the caller; there is no network access, optimization or hidden holdout
capability. A bar indexed at t is the candle OPEN covering [t,t+1 min).
Thus its OHLC is only usable at t+1 min. The confirmation signal is
recorded at that completion instant, and the entry is the OPEN of the
second following bar, t+2 min, not an intrabar assumed fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter

import numpy as np
import pandas as pd

from lab.crash_execution import fill_accounting

MODEL = "btc-failed-support-retest-short-frozen-v1"
SIDE = -1
SUPPORT_MINUTES = 1440
VOLUME_MINUTES = 60
PRIOR_HOURS = 7 * 24
BREACH_FRACTION = 0.003
VOLUME_MULTIPLE = 2.0
RETEST_DISTANCE = 0.001
RETEST_MINUTES = 60
CONFIRM_MINUTES = 5
CONFIRM_DROP = 0.001
STOP_BUFFER = 0.001
REWARD_TO_RISK = 2.0
MAX_HOLD_MINUTES = 240
FEE_BPS = 5.0
SLIPPAGE_BPS = 5.0
VOL_TARGET = 0.5
MAX_WEIGHT = 2.0
MINUTES_PER_YEAR = 365 * 1440
UTC_MIN = pd.Timedelta(minutes=1)


@dataclass
class Result:
    trades: pd.DataFrame
    event_counts: dict[str, int]
    boundary: pd.Timestamp
    model: str = MODEL


def _check(b: pd.DataFrame, boundary: pd.Timestamp) -> pd.Timestamp:
    boundary = pd.Timestamp(boundary)
    if boundary.tzinfo is None or boundary.utcoffset() != pd.Timedelta(0):
        raise ValueError("a fixed UTC holdout boundary is required")
    if boundary != boundary.floor("D"):
        raise ValueError("the holdout boundary must be UTC midnight")
    if b.empty or not isinstance(b.index, pd.DatetimeIndex):
        raise ValueError("minute bar index is required")
    if b.index.tz is None or b.index[0].utcoffset() != pd.Timedelta(0):
        raise ValueError("bar timestamps must be UTC")
    if not b.index.is_monotonic_increasing or not b.index.is_unique:
        raise ValueError("bars must have unique ordered timestamps")
    if b.index[-1] + UTC_MIN > boundary:
        raise ValueError("refusing to analyze a bar overlapping protected holdout")
    if len(b) > 1 and not bool((b.index[1:] - b.index[:-1] == UTC_MIN).all()):
        raise ValueError("missing one-minute source bar; cannot bridge unknown price path")
    required = {"open", "high", "low", "close", "quote_volume", "oi_invalid"}
    if not required <= set(b.columns):
        raise ValueError("missing OHLC, quote volume or OI-source-quality column")
    a = b[["open", "high", "low", "close", "quote_volume"]].to_numpy(dtype=float)
    if not np.isfinite(a).all():
        raise ValueError("nonfinite price or quote-volume data")
    for col in ("open", "high", "low", "close"):
        if (b[col].to_numpy(dtype=float) <= 0).any():
            raise ValueError("nonpositive candle reference price")
    if (b["quote_volume"].to_numpy(dtype=float) < 0).any():
        raise ValueError("negative quote volume")
    if (
        (b["high"] < b[["open", "close"]].max(axis=1)).any()
        or (b["low"] > b[["open", "close"]].min(axis=1)).any()
        or (b["low"] > b["high"]).any()
    ):
        raise ValueError("inconsistent minute OHLC candle")
    invalid = b["oi_invalid"]
    if invalid.isna().any() or not invalid.isin([True, False]).all():
        raise ValueError("OI-source-quality markers must be explicit booleans")
    return boundary


def simulate(b: pd.DataFrame, *, boundary: pd.Timestamp) -> Result:
    """One frozen candidate; all cancellations are counted, never optimized."""
    boundary = _check(b, boundary)
    count = Counter()
    empty_cols = [
        "signal", "entry", "exit", "gross", "net", "weight", "reason",
        "entry_reference", "exit_reference", "fee_cost", "slippage_cost",
        "breakout", "retest", "support", "stop_reference", "target_reference",
    ]
    if len(b) < (PRIOR_HOURS + 1) * 60 + SUPPORT_MINUTES:
        return Result(pd.DataFrame(columns=empty_cols), {}, boundary)

    # A complete hourly bucket has exactly sixty valid original minute bars.
    hourly = b["quote_volume"].resample("h").agg(["sum", "count"])
    hourly["sum"] = hourly["sum"].where(hourly["count"] == 60)
    hourly_median = hourly["sum"].rolling(PRIOR_HOURS, min_periods=PRIOR_HOURS).median()
    close = b["close"]
    low = b["low"]
    q = b["quote_volume"]
    support = low.shift(1).rolling(SUPPORT_MINUTES, min_periods=SUPPORT_MINUTES).min()
    recent_volume = q.rolling(VOLUME_MINUTES, min_periods=VOLUME_MINUTES).sum()
    oi_bad = b["oi_invalid"].astype(bool).to_numpy()
    oi_24h_invalid = b["oi_invalid"].astype(bool).rolling(
        SUPPORT_MINUTES + 1, min_periods=SUPPORT_MINUTES + 1,
    ).max().fillna(1).to_numpy(dtype=bool)

    log_return = np.log(close).diff()
    # Use the preceding seven days, delayed by one full hour:
    # the breakout move itself cannot affect position sizing.
    sigma = log_return.rolling(
        PRIOR_HOURS * 60, min_periods=PRIOR_HOURS * 60,
    ).std().shift(VOLUME_MINUTES) * np.sqrt(MINUTES_PER_YEAR)
    weights = np.minimum(VOL_TARGET / sigma, MAX_WEIGHT).fillna(0).to_numpy()
    o, h, l, c = (b[key].to_numpy(dtype=float) for key in ("open", "high", "low", "close"))
    rows = []
    i = (PRIOR_HOURS + 1) * 60 + SUPPORT_MINUTES
    n = len(b)
    while i < n:
        L = float(support.iloc[i])
        if (
            not np.isfinite(L) or not (c[i] <= (1 - BREACH_FRACTION) * L)
            or oi_24h_invalid[i]
        ):
            i += 1
            continue
        # Last 60-minute volume includes candles [i-59, i]. Only
        # hourly buckets ending <= start of that window are eligible
        # as prior-seven-day reference; there is no lookahead or overlap.
        start_recent = b.index[i] - pd.Timedelta(minutes=VOLUME_MINUTES - 1)
        last_prior_hour = start_recent.floor("h") - pd.Timedelta(hours=1)
        typical = hourly_median.get(last_prior_hour, float("nan"))
        if not np.isfinite(typical) or typical <= 0 or recent_volume.iloc[i] < VOLUME_MULTIPLE * typical:
            i += 1
            continue

        count["breakout"] += 1
        breakout_index = i
        retest_i = None
        cause = "no_retest"
        for j in range(i + 1, min(n, i + RETEST_MINUTES + 1)):
            if oi_bad[j]:
                cause = "untrusted_oi"
                break
            if c[j] > L:
                cause = "support_reclaimed"
                break
            if h[j] >= (1 - RETEST_DISTANCE) * L:
                retest_i = j
                break
        if retest_i is None:
            count[cause] += 1
            i = max(i + 1, min(n, i + RETEST_MINUTES + 1))
            continue
        count["retest"] += 1
        confirm_i = None
        cause = "no_confirmation"
        for k in range(retest_i + 1, min(n, retest_i + CONFIRM_MINUTES + 1)):
            if oi_bad[k]:
                cause = "untrusted_oi"
                break
            if c[k] > L:
                cause = "support_reclaimed"
                break
            if c[k] < (1 - CONFIRM_DROP) * l[retest_i]:
                confirm_i = k
                break
        if confirm_i is None:
            count[cause] += 1
            i = max(i + 1, min(n, retest_i + CONFIRM_MINUTES + 1))
            continue
        count["confirmation"] += 1
        # Bar k is complete at k+1min. Entry at open of the
        # second subsequent bar (index k+2), never at close[k].
        entry_i = confirm_i + 2
        if entry_i >= n:
            count["late_entry"] += 1
            break
        P = o[entry_i]
        stop = (1 + STOP_BUFFER) * max(L, h[retest_i])
        target = P - REWARD_TO_RISK * (stop - P)
        if stop <= P:
            count["invalid_stop"] += 1
            i = entry_i + 1
            continue
        if target <= 0:
            count["invalid_target"] += 1
            i = entry_i + 1
            continue
        weight = float(weights[breakout_index])
        if not np.isfinite(weight) or weight <= 0:
            count["unavailable_volatility_sizing"] += 1
            i = entry_i + 1
            continue

        exit_i = None
        exit_ref = None
        reason = None
        for m in range(entry_i, min(n, entry_i + MAX_HOLD_MINUTES)):
            if h[m] >= stop:  # also wins over a target hit in same minute
                exit_i, exit_ref, reason = m, max(stop, o[m]), "stop"
                break
            if l[m] <= target:
                exit_i, exit_ref, reason = m, target, "target"
                break
            if m == entry_i + MAX_HOLD_MINUTES - 1:
                exit_i, exit_ref, reason = m, c[m], "time"
                break
        if exit_i is None:
            count["unresolved_end_of_research"] += 1
            break
        executed = fill_accounting(SIDE, P, exit_ref, FEE_BPS, SLIPPAGE_BPS)
        count["trade"] += 1
        rows.append((
            b.index[confirm_i] + UTC_MIN,
            b.index[entry_i],
            b.index[exit_i] + UTC_MIN,
            executed["reference_gross"], executed["net"], weight, reason,
            P, exit_ref, executed["fee_cost"], executed["slippage_cost"],
            b.index[breakout_index] + UTC_MIN, b.index[retest_i] + UTC_MIN,
            L, stop, target,
        ))
        # One symbol: no overlapping candidate or position.
        i = exit_i + 1
    return Result(pd.DataFrame(rows, columns=empty_cols), dict(sorted(count.items())), boundary)
