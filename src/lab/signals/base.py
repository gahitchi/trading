"""
The signal interface: the ONLY thing a strategy author (human or AI) writes.

A Signal sees a read-only window of past daily bars, ending with the bar that
just closed, and returns its desired exposure to that stock:

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
    """Read-only numpy arrays of the most recent `len(window)` daily bars, oldest first."""

    __slots__ = ("open", "high", "low", "close", "volume")

    def __init__(self, o, h, lo, c, v):
        for name, arr in (("open", o), ("high", h), ("low", lo), ("close", c), ("volume", v)):
            a = np.asarray(arr, dtype=float)
            a.setflags(write=False)
            object.__setattr__(self, name, a)

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
    """Coerce a signal's output into [0, 1]; anything invalid becomes 0 (fail flat)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x):
        return 0.0
    return min(1.0, max(0.0, x))


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
