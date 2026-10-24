"""A frozen, past-selected schedule executed in one continuous portfolio book."""

import numpy as np

from lab.signals.base import CrossSectionalSignal, Signal, Window, clamp_target, clean_weights


def tail(w, n):
    return Window(w.open[-n:], w.high[-n:], w.low[-n:], w.close[-n:], w.volume[-n:], w.dates[-n:], w.minute[-n:])


def warm_up(lookback: int) -> np.timedelta64:
    """Calendar days before its start date over which a scheduled signal is already run (unused), so a
    stateful one starts with state built on at least `lookback` past bars."""
    return np.timedelta64(int(lookback * 1.5) + 10, "D")


class _Schedule:
    """Only the active entry and, inside its warm-up, the next one are evaluated on a bar (Gemini audit F-R14):
    a 15-window study no longer runs 15 signals (15 sandboxed workers for research signals) on every bar.
    Entries whose window has passed are closed."""

    def __init__(self, schedule):
        self.entries = [(np.datetime64(day), spec.make()) for day, spec in schedule]
        self.closed = 0

    def due(self, now) -> tuple[list[int], int]:
        """(indices to evaluate, index of the active entry or -1)."""
        active = -1
        for i, (day, _) in enumerate(self.entries):
            if now >= day:
                active = i
        for i in range(self.closed, max(active, 0)):
            sig = self.entries[i][1]
            if hasattr(sig, "close"):
                sig.close()
        self.closed = max(self.closed, active)
        run = [active] if active >= 0 else []
        nxt = active + 1
        if nxt < len(self.entries):
            day, sig = self.entries[nxt]
            if now >= day - warm_up(sig.lookback):
                run.append(nxt)
        return run, active

    def close(self):
        for _, sig in self.entries[self.closed :]:
            if hasattr(sig, "close"):
                sig.close()


class ScheduledSignal(Signal):
    name = "walk_forward_policy"

    def __init__(self, schedule):
        super().__init__()
        self.plan = _Schedule(schedule)
        self.schedule = self.plan.entries
        if any(getattr(sig, "cross_sectional", False) for _, sig in self.schedule):
            raise ValueError("use ScheduledCrossSectional for cross-sectional policies")
        self.lookback = max(sig.lookback for _, sig in self.schedule)

    def close(self):
        self.plan.close()

    def target(self, w):
        run, active = self.plan.due(w.dates[-1])
        out = 0.0
        for i in run:
            sig = self.schedule[i][1]
            result = float(sig.target(tail(w, sig.lookback)))
            if not np.isfinite(result):
                raise ValueError("nonfinite scheduled signal")
            sig.last = clamp_target(result)
            if i == active:
                out = sig.last
        return out


class ScheduledCrossSectional(CrossSectionalSignal):
    name = "walk_forward_policy"

    def __init__(self, schedule):
        super().__init__()
        self.plan = _Schedule(schedule)
        self.schedule = self.plan.entries
        if any(not getattr(sig, "cross_sectional", False) for _, sig in self.schedule):
            raise ValueError("cannot mix single-stock and cross-sectional selection in one policy")
        self.lookback = max(sig.lookback for _, sig in self.schedule)

    def close(self):
        self.plan.close()

    def weights(self, windows):
        if not windows:
            return {}
        run, active = self.plan.due(min(w.dates[-1] for w in windows.values()))
        out = {}
        for i in run:
            sig = self.schedule[i][1]
            raw = sig.weights({t: tail(w, sig.lookback) for t, w in windows.items()})
            if any(not np.isfinite(float(v)) for v in raw.values()):
                raise ValueError("nonfinite scheduled weights")
            sig.last = clean_weights(raw)
            if i == active:
                out = sig.last
        return out
