"""Deterministic, outcome-blind size/duration matched placebo for crash research.

For every original trade, sample a different UTC day in its calendar
month at its exact UTC time of day. Use its SAME initial-notional weight
and realized holding duration, but a fresh observed entry open and exit
close. Sampling never inspects candidate returns and forbids overlapping
placebo exposures. Since durations arise from the original strategy's
realized exits, this is a DESCRIPTIVE signal-timing comparator, not a
tradable stop/target strategy or a valid statistical null distribution.
"""

from __future__ import annotations

import calendar
import math
import random

import numpy as np
import pandas as pd

from lab.crash_execution import fill_accounting

POLICY = "month-hour-size-duration-matched-calendar-placebo-v1"
SEEDS = tuple(20261105 + 997 * i for i in range(16))
MAX_DAY_OFFSET = 14
MIN_DAY_SEPARATION = 3


def matched_placebo(
    bars: pd.DataFrame,
    reference: pd.DataFrame,
    *,
    side: int,
    fee_bps: float,
    slippage_bps: float,
    seed: int,
) -> pd.DataFrame:
    """One calendar-matched control per original trade, or fail closed."""
    if type(seed) is not int or type(side) is not int or side not in (-1, 1):
        raise ValueError("placebo seed and side must be fixed integers")
    required = {"signal", "entry", "exit", "weight", "net"}
    if not required <= set(reference) or not {"open", "high", "low", "close"} <= set(bars):
        raise ValueError("missing original trades or minute bars")
    if bars.empty or bars.index.tz is None or not bars.index.is_monotonic_increasing or not bars.index.is_unique:
        raise ValueError("invalid placebo input minute grid")
    if bars.index[0].utcoffset() != pd.Timedelta(0):
        raise ValueError("placebo requires UTC bars")
    rng = random.Random(seed)
    chosen = []
    rows = []
    for original in reference.itertuples(index=False):
        origin = pd.Timestamp(original.entry)
        original_exit = pd.Timestamp(original.exit)
        original_signal = pd.Timestamp(original.signal)
        if any(t.tzinfo is None or t.utcoffset() != pd.Timedelta(0) for t in (origin, original_exit, original_signal)):
            raise ValueError("placebo requires UTC original trade times")
        duration = (original_exit - origin).total_seconds() / 60
        if duration < 1 or int(duration) != duration:
            raise ValueError("placebo requires integral-minute positive exposure")
        minutes = int(duration)
        delay = origin - original_signal
        if delay < pd.Timedelta(0):
            raise ValueError("placebo original signal comes after entry")
        if not math.isfinite(float(original.weight)) or float(original.weight) <= 0:
            raise ValueError("placebo requires positive finite original weight")

        last_day = calendar.monthrange(origin.year, origin.month)[1]
        offsets = [
            day - origin.day
            for day in range(1, last_day + 1)
            if MIN_DAY_SEPARATION <= abs(day - origin.day) <= MAX_DAY_OFFSET
        ]
        rng.shuffle(offsets)
        selected = None
        for offset in offsets:
            entry = origin + pd.Timedelta(days=offset)
            exit_ = entry + pd.Timedelta(minutes=minutes)
            if any(not (exit_ <= previous_entry or entry >= previous_exit) for previous_entry, previous_exit in chosen):
                continue
            first_bar = entry + pd.Timedelta(minutes=1)
            if first_bar not in bars.index or exit_ not in bars.index:
                continue
            chunk = bars.loc[first_bar:exit_, ["open", "high", "low", "close"]]
            if len(chunk) != minutes:
                continue
            numeric = chunk.to_numpy(dtype=float)
            if not np.isfinite(numeric).all() or not (numeric > 0).all():
                continue
            # Only data availability, not returns or P&L, determines
            # eligibility. Never optimize against the original result.
            selected = (entry, exit_, float(chunk["open"].iloc[0]), float(chunk["close"].iloc[-1]))
            break
        if selected is None:
            raise ValueError("no valid nonoverlapping same-month/hour exposure-matched placebo for trade")
        entry, exit_, buy_ref, sell_ref = selected
        account = fill_accounting(side, buy_ref, sell_ref, fee_bps, slippage_bps)
        chosen.append((entry, exit_))
        rows.append(
            {
                "signal": entry - delay,
                "entry": entry,
                "exit": exit_,
                "gross": account["reference_gross"],
                "net": account["net"],
                "weight": float(original.weight),
                "reason": "fixed_duration_placebo",
                "entry_reference": buy_ref,
                "exit_reference": sell_ref,
                "fee_cost": account["fee_cost"],
                "slippage_cost": account["slippage_cost"],
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "signal",
            "entry",
            "exit",
            "gross",
            "net",
            "weight",
            "reason",
            "entry_reference",
            "exit_reference",
            "fee_cost",
            "slippage_cost",
        ],
    )


def comparison(strategy: pd.DataFrame, control: pd.DataFrame) -> dict:
    """Describe paired notional exposures; NEVER return a p-value."""
    if len(strategy) != len(control):
        raise ValueError("placebo must preserve exact original trade count")
    if len(strategy) == 0:
        return {"trades": 0, "status": "unavailable"}
    s_minutes = (strategy["exit"] - strategy["entry"]).dt.total_seconds() / 60
    c_minutes = (control["exit"] - control["entry"]).dt.total_seconds() / 60
    if not np.array_equal(s_minutes.to_numpy(), c_minutes.to_numpy()) or not np.allclose(
        strategy["weight"].to_numpy(dtype=float),
        control["weight"].to_numpy(dtype=float),
        atol=0,
        rtol=0,
    ):
        raise ValueError("control is not duration/size matched to strategy")
    return {
        "trades": len(strategy),
        "status": "descriptive_no_hypothesis_test",
        "identical_entry_notional_weights": True,
        "identical_realized_exposure_minutes": True,
        "original_mean_net_bps": float(strategy.net.mean() * 1e4),
        "control_mean_net_bps": float(control.net.mean() * 1e4),
        "paired_mean_difference_bps": float((strategy.net - control.net).mean() * 1e4),
        "original_weighted_net_bps": float((strategy.net * strategy.weight).sum() * 1e4),
        "control_weighted_net_bps": float((control.net * control.weight).sum() * 1e4),
        "limitation": (
            "Control positions have fixed original realized durations and "
            "no stop/target exits: an exposure-matched descriptive placebo, "
            "not a tradable alternative or inference-calibrated null."
        ),
    }
