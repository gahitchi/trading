"""A walk-forward policy invokes only its currently selected model.

Regression for external audit F-R14: dispatching every expired/future signal
wastes isolated-worker IPC and lets unselected signals halt a valid strategy.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from lab.signals.base import CrossSectionalSignal, Signal, Window
from lab.signals.walk_forward import ScheduledCrossSectional, ScheduledSignal


class CountedSignal(Signal):
    lookback = 3

    def __init__(self, value):
        super().__init__()
        self.value = value
        self.calls = 0
        self.closes = 0

    def target(self, w):
        self.calls += 1
        assert len(w.close) == self.lookback
        return self.value

    def close(self):
        self.closes += 1


class CountedCross(CrossSectionalSignal):
    lookback = 3

    def __init__(self, value):
        super().__init__()
        self.value = value
        self.calls = 0
        self.closes = 0

    def weights(self, windows):
        self.calls += 1
        assert all(len(w.close) == self.lookback for w in windows.values())
        return {t: self.value for t in windows}

    def close(self):
        self.closes += 1


def spec(signal):
    return SimpleNamespace(make=lambda: signal)


def window(last):
    days = np.arange("2025-01-01", np.datetime64(last) + np.timedelta64(1, "D"), dtype="datetime64[D]")
    values = np.arange(1, len(days) + 1, dtype=float)
    return Window(values, values, values, values, values, days)


def test_single_stock_dispatch_skips_expired_and_future_workers():
    first, second, third = CountedSignal(0.3), CountedSignal(-0.4), CountedSignal(0.8)
    policy = ScheduledSignal([("2025-01-02", spec(first)), ("2025-01-05", spec(second)), ("2025-01-08", spec(third))])
    assert policy.target(window("2025-01-03")) == pytest.approx(0.3)
    assert (first.calls, second.calls, third.calls) == (1, 0, 0)
    assert policy.target(window("2025-01-06")) == pytest.approx(-0.4)
    assert (first.calls, second.calls, third.calls) == (1, 1, 0)
    assert policy.target(window("2025-01-09")) == pytest.approx(0.8)
    assert (first.calls, second.calls, third.calls) == (1, 1, 1)
    policy.close()
    assert [s.closes for s in (first, second, third)] == [1, 1, 1]


def test_unselected_invalid_signal_does_not_abort_selected_policy():
    first, later = CountedSignal(float("nan")), CountedSignal(0.5)
    policy = ScheduledSignal([("2025-01-02", spec(first)), ("2025-01-08", spec(later))])
    assert policy.target(window("2025-01-09")) == pytest.approx(0.5)
    assert first.calls == 0
    assert later.calls == 1
    with pytest.raises(ValueError, match="nonfinite scheduled signal"):
        policy.target(window("2025-01-04"))


def test_no_active_schedule_returns_cash_without_worker_calls():
    future = CountedSignal(1)
    policy = ScheduledSignal([("2025-01-10", spec(future))])
    assert policy.target(window("2025-01-04")) == 0
    assert future.calls == 0


def test_cross_sectional_dispatch_skips_nonselected_models():
    past, active, future = CountedCross(0.9), CountedCross(-0.2), CountedCross(0.6)
    policy = ScheduledCrossSectional(
        [("2025-01-02", spec(past)), ("2025-01-05", spec(active)), ("2025-01-12", spec(future))]
    )
    windows = {"A": window("2025-01-07"), "B": window("2025-01-06")}
    assert policy.weights(windows) == {"A": -0.2, "B": -0.2}
    assert [s.calls for s in (past, active, future)] == [0, 1, 0]
    assert policy.weights({}) == {}
    assert policy.weights({"A": window("2025-01-01")}) == {}
    assert [s.calls for s in (past, active, future)] == [0, 1, 0]
    policy.close()
    assert all(s.closes == 1 for s in (past, active, future))


def test_nonfinite_active_cross_sectional_model_still_fails_closed():
    bad = CountedCross(float("nan"))
    policy = ScheduledCrossSectional([("2025-01-02", spec(bad))])
    with pytest.raises(ValueError, match="nonfinite scheduled weights"):
        policy.weights({"A": window("2025-01-04")})
