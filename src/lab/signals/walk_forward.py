"""A frozen, past-selected schedule executed in one continuous portfolio book."""

import numpy as np

from lab.signals.base import CrossSectionalSignal, Signal, Window, clamp_target, clean_weights


def tail(w, n):
    return Window(w.open[-n:], w.high[-n:], w.low[-n:], w.close[-n:], w.volume[-n:], w.dates[-n:], w.minute[-n:])


def warm_up(lookback: int) -> np.timedelta64:
    """Calendar days before its start date over which a scheduled signal is already run (unused), so a
    stateful one starts with state built on at least `lookback` past bars."""
    return np.timedelta64(int(lookback * 1.5) + 10, "D")


def _warm(sig, call, finish) -> None:
    """Run an entry that is not active yet so its state (including `last`) builds up. A failure or a nonfinite
    output is ignored: an entry that is not selected must not veto the one that is (ChatGPT, G014)."""
    try:
        sig.last = finish(call())
    except Exception:  # an unselected entry's failure is not the policy's
        return


def _finish_target(result):
    value = float(result)
    if not np.isfinite(value):
        raise ValueError("nonfinite")
    return clamp_target(value)


def _finish_weights(raw):
    if any(not np.isfinite(float(v)) for v in raw.values()):
        raise ValueError("nonfinite")
    return clean_weights(raw)


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
            if i != active:
                _warm(sig, lambda s=sig: s.target(tail(w, s.lookback)), _finish_target)  # output not used yet
                continue
            result = float(sig.target(tail(w, sig.lookback)))
            if not np.isfinite(result):
                raise ValueError("nonfinite scheduled signal")
            sig.last = clamp_target(result)
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
            if i != active:
                _warm(
                    sig, lambda s=sig: s.weights({t: tail(w, s.lookback) for t, w in windows.items()}), _finish_weights
                )
                continue
            raw = sig.weights({t: tail(w, sig.lookback) for t, w in windows.items()})
            if any(not np.isfinite(float(v)) for v in raw.values()):
                raise ValueError("nonfinite scheduled weights")
            sig.last = clean_weights(raw)
            out = sig.last
        return out
