"""
The signal interface: the ONLY thing a strategy author (human or AI) writes.

A Signal sees a read-only window of past daily bars (prices, volume and session
dates), ending with the bar that just closed, and returns its desired exposure:

    0.0 = no position      1.0 = fully invested (its share of the portfolio)

Everything else (sizing, risk limits, orders, fills, costs) is handled by the
deterministic agents. Because the window physically contains no future bars,
look-ahead bias is impossible by construction.

Signals may keep state between calls (e.g. "am I in a trade?"), but must be
deterministic: same bars in the same order -> same targets.
"""

from __future__ import annotations

import math

import numpy as np


class Window:
    """Read-only numpy arrays of the most recent `len(window)` daily bars, oldest first.

    open, high, low, close, volume: float arrays
    dates: numpy datetime64[D] array of the session dates (New York), for calendar
           effects; e.g. day of month: (w.dates - w.dates.astype("datetime64[M]")).astype(int) + 1
    """

    __slots__ = ("close", "dates", "high", "low", "open", "volume")

    def __init__(self, o, h, lo, c, v, dates=None):
        for name, arr in (("open", o), ("high", h), ("low", lo), ("close", c), ("volume", v)):
            a = np.asarray(arr, dtype=float)
            a.setflags(write=False)
            object.__setattr__(self, name, a)
        d = (
            np.asarray(dates, dtype="datetime64[D]")
            if dates is not None
            else np.full(len(self.close), np.datetime64("NaT", "D"))
        )
        d.setflags(write=False)
        object.__setattr__(self, "dates", d)

    def __setattr__(self, *_):
        raise AttributeError("Window is read-only")

    def __len__(self) -> int:
        return len(self.close)


class Signal:
    name: str = "unnamed"
    description: str = ""
    lookback: int = 1  # bars required before target() is first called (max 250)
    defaults: dict = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.params = {**self.defaults, **params}
        self.last = 0.0  # the target returned on the previous call

    def target(self, w: Window) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def label(self) -> str:
        if not self.params:
            return self.name
        return f"{self.name}(" + ",".join(f"{k}={v}" for k, v in sorted(self.params.items())) + ")"


def clamp_target(x) -> float:
    """Coerce a signal's output into [-1, 1]; anything invalid becomes 0 (fail flat).

    Negative = short. Whether a short is ever placed is decided by the risk limits
    (lab.toml [risk] max_short_gross, 0 by default), not by the signal."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x):
        return 0.0
    return min(1.0, max(-1.0, x))


# ----------------------------------------------------------------- helpers for authors


def sma(x: np.ndarray, n: int) -> float:
    return float(np.mean(x[-n:]))


def rsi(close: np.ndarray, n: int) -> float:
    """Wilder's RSI over the whole window (needs len(close) > n)."""
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:n].mean()
    ad = dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    if ad == 0:
        return 100.0
    return float(100 - 100 / (1 + au / ad))


class CrossSectionalSignal:
    """A strategy that ranks stocks against each other (e.g. momentum: buy the past winners).

    Once per day it sees a Window for every stock currently in the universe that has at
    least `lookback` bars, and returns weights: fractions of this strategy's capital,
    negative for a short, with |weights| summing to at most 1. Stocks it leaves out get 0.
    """

    name: str = "unnamed"
    description: str = ""
    lookback: int = 1
    defaults: dict = {}
    cross_sectional = True

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.params = {**self.defaults, **params}
        self.last: dict[str, float] = {}

    def weights(self, windows: dict[str, Window]) -> dict[str, float]:  # pragma: no cover - interface
        raise NotImplementedError

    def label(self) -> str:
        if not self.params:
            return self.name
        return f"{self.name}(" + ",".join(f"{k}={v}" for k, v in sorted(self.params.items())) + ")"


def clean_weights(raw) -> dict[str, float]:
    """Coerce a cross-sectional output into valid signed weights whose |weights| sum to <= 1
    (the strategy's own capital; leverage is a portfolio decision, not the strategy's)."""
    out = {}
    for k, v in (raw or {}).items():
        x = clamp_target(v) if not isinstance(v, bool) else float(v)
        if x != 0:
            out[str(k)] = x
    gross = sum(abs(v) for v in out.values())
    return {k: v / gross for k, v in out.items()} if gross > 1 else out
