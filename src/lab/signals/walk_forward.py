"""A frozen, past-selected schedule executed in one continuous portfolio book."""

import numpy as np

from lab.signals.base import CrossSectionalSignal, Signal, Window, clamp_target, clean_weights


def tail(w, n):
    return Window(w.open[-n:], w.high[-n:], w.low[-n:], w.close[-n:], w.volume[-n:], w.dates[-n:], w.minute[-n:])


class ScheduledSignal(Signal):
    name = "walk_forward_policy"

    def __init__(self, schedule):
        super().__init__()
        self.schedule = [(np.datetime64(day), spec.make()) for day, spec in schedule]
        if any(getattr(sig, "cross_sectional", False) for _, sig in self.schedule):
            raise ValueError("use ScheduledCrossSectional for cross-sectional policies")
        self.lookback = max(sig.lookback for _, sig in self.schedule)

    def close(self):
        for _, sig in self.schedule:
            if hasattr(sig, "close"):
                sig.close()

    def target(self, w):
        out = 0.0
        for day, sig in self.schedule:
            result = float(sig.target(tail(w, sig.lookback)))
            if not np.isfinite(result):
                raise ValueError("nonfinite scheduled signal")
            sig.last = clamp_target(result)
            if w.dates[-1] >= day:
                out = sig.last
        return out


class ScheduledCrossSectional(CrossSectionalSignal):
    name = "walk_forward_policy"

    def __init__(self, schedule):
        super().__init__()
        self.schedule = [(np.datetime64(day), spec.make()) for day, spec in schedule]
        if any(not getattr(sig, "cross_sectional", False) for _, sig in self.schedule):
            raise ValueError("cannot mix single-stock and cross-sectional selection in one policy")
        self.lookback = max(sig.lookback for _, sig in self.schedule)

    def close(self):
        for _, sig in self.schedule:
            if hasattr(sig, "close"):
                sig.close()

    def weights(self, windows):
        if not windows:
            return {}
        now = min(w.dates[-1] for w in windows.values())
        out = {}
        for day, sig in self.schedule:
            raw = sig.weights({t: tail(w, sig.lookback) for t, w in windows.items()})
            if any(not np.isfinite(float(v)) for v in raw.values()):
                raise ValueError("nonfinite scheduled weights")
            sig.last = clean_weights(raw)
            if now >= day:
                out = sig.last
        return out
