"""Vectorized, columnar historical UTC minute aggregation (NOT order execution).

The streaming MinuteBuilder remains a small event-watermark state machine.
Historical research uses NumPy reductions rather than allocating one Python
Trade/MinuteBar object for every input tick or every minute. Float64 prices
are appropriate for feature extraction, NOT broker monetary accounting.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MINUTE_NS = 60_000_000_000


@dataclass(frozen=True, slots=True)
class MinuteArrays:
    start_ns: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    trades: np.ndarray
    missing_minutes_before: np.ndarray


def aggregate_trades(
    timestamps_ns: np.ndarray,
    prices: np.ndarray,
    quantities: np.ndarray,
) -> MinuteArrays:
    """Return only observed minutes, in UTC, without filling missing bars."""
    stamp = np.asarray(timestamps_ns)
    price = np.asarray(prices)
    qty = np.asarray(quantities)
    if stamp.ndim != 1 or price.ndim != 1 or qty.ndim != 1 or not (len(stamp) == len(price) == len(qty)):
        raise ValueError("timestamps, prices and quantities must be equally sized 1D arrays")
    if not len(stamp):
        raise ValueError("empty trade sequence")
    if stamp.dtype != np.dtype("int64"):
        raise TypeError("timestamps must be exact int64 Unix nanoseconds")
    if price.dtype.kind not in {"f", "i", "u"} or qty.dtype.kind not in {"f", "i", "u"}:
        raise TypeError("prices and quantities must be numeric arrays")
    if np.any(stamp < 0) or np.any(stamp[1:] < stamp[:-1]):
        raise ValueError("negative or out-of-order exchange timestamps")
    price = price.astype(np.float64, copy=False)
    qty = qty.astype(np.float64, copy=False)
    if not np.all(np.isfinite(price)) or not np.all(np.isfinite(qty)):
        raise ValueError("nonfinite price or quantity")
    if np.any(price <= 0) or np.any(qty <= 0):
        raise ValueError("trade price and quantity must be strictly positive")
    minute = stamp // MINUTE_NS * MINUTE_NS
    starts, indices, counts = np.unique(minute, return_index=True, return_counts=True)
    last = indices + counts - 1
    gaps = np.zeros(len(starts), dtype=np.int64)
    gaps[1:] = (starts[1:] - starts[:-1]) // MINUTE_NS - 1
    return MinuteArrays(
        start_ns=starts,
        open=price[indices],
        high=np.maximum.reduceat(price, indices),
        low=np.minimum.reduceat(price, indices),
        close=price[last],
        volume=np.add.reduceat(qty, indices),
        trades=counts,
        missing_minutes_before=gaps,
    )


@dataclass(frozen=True, slots=True)
class TradingCadence:
    """Alpha and execution resolutions are orthogonal; no new agent stack."""

    decision_minutes: int = 60
    execution_minutes: int = 1

    def __post_init__(self) -> None:
        if type(self.decision_minutes) is not int or self.decision_minutes not in {15, 60, 240, 1440}:
            raise ValueError("decision resolution must be 15m, 1h, 4h, or 1d")
        if self.execution_minutes != 1:
            raise ValueError("D1 monitoring/execution resolution must be one minute")
