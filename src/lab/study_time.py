"""Recorded-book coverage for studies: a silence over one second starts a new segment.

Both venues normally update within 100 ms. This conservative limit discards uncertain observations; a recorder
outage is never an unchanged price or an interval in which a hypothetical order can keep resting.
"""

import math

import numpy as np

MAX_BOOK_GAP_NS = 1_000_000_000


def segments(ts):
    return np.cumsum(np.r_[0, np.diff(ts) > MAX_BOOK_GAP_NS]) if len(ts) else np.array([], dtype=np.int64)


def future_mids(ts, prices, starts, horizon_s: float, segment=None):
    """Last mid at or before start + horizon, only within one continuously observed segment.

    A target inside a hole is unavailable, even if a mid exists before it. `segment` may come from the full book
    stream when `ts` contains only sampled observations, so a slow sampling clock doesn't invent recorder holes.
    """
    if not math.isfinite(horizon_s) or horizon_s < 0:
        raise ValueError("horizon must be finite and nonnegative")
    ts, prices, starts = np.asarray(ts), np.asarray(prices), np.asarray(starts)
    result = np.full(len(starts), np.nan)
    if not len(ts):
        return result
    segment = segments(ts) if segment is None else np.asarray(segment)
    targets = starts + int(horizon_s * 1e9)
    begin = np.searchsorted(ts, starts, side="right") - 1
    end = np.searchsorted(ts, targets, side="right") - 1
    valid = (begin >= 0) & (end >= 0) & (targets <= ts[-1])
    begin, end = begin.clip(0, len(ts) - 1), end.clip(0, len(ts) - 1)
    valid &= segment[begin] == segment[end]
    for pos, times in ((begin, starts), (end, targets)):
        after = (pos + 1).clip(0, len(ts) - 1)
        valid &= ~((times > ts[pos]) & (segment[after] != segment[pos]))
    result[valid] = prices[end[valid]]
    return result


def recorded_hours(ts):
    """Duration within recorded segments; time between sessions contributes nothing."""
    elapsed = np.diff(ts)
    return float(elapsed[elapsed <= MAX_BOOK_GAP_NS].sum() / 3.6e12)
