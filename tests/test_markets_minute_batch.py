"""Columnar research aggregation must not fabricate missing 24/7 crypto minutes."""

from __future__ import annotations

import numpy as np
import pytest

from lab.markets.minute_batch import MINUTE_NS, TradingCadence, aggregate_trades


def test_vectorized_utc_minutes_and_missing_intervals() -> None:
    stamps = np.array([1, 2, MINUTE_NS + 3, 4 * MINUTE_NS + 1], dtype=np.int64)
    prices = np.array([100, 110, 90, 105], dtype=np.float64)
    volumes = np.array([1, 2, 3, 4], dtype=np.float64)
    out = aggregate_trades(stamps, prices, volumes)
    np.testing.assert_array_equal(out.start_ns, [0, MINUTE_NS, 4 * MINUTE_NS])
    np.testing.assert_array_equal(out.open, [100, 90, 105])
    np.testing.assert_array_equal(out.high, [110, 90, 105])
    np.testing.assert_array_equal(out.low, [100, 90, 105])
    np.testing.assert_array_equal(out.close, [110, 90, 105])
    np.testing.assert_array_equal(out.volume, [3, 3, 4])
    np.testing.assert_array_equal(out.trades, [2, 1, 1])
    np.testing.assert_array_equal(out.missing_minutes_before, [0, 0, 2])


def test_one_year_columnar_aggregation_without_python_bar_instances() -> None:
    # 365 x 1440 minutes, 525,600 bars per 24/7 year. No timing assertion on CI hardware.
    stamps = np.arange(525_600, dtype=np.int64) * MINUTE_NS
    value = np.full(len(stamps), 100.0)
    qty = np.ones(len(stamps))
    bars = aggregate_trades(stamps, value, qty)
    assert isinstance(bars.close, np.ndarray)
    assert len(bars.close) == 525_600
    assert bars.start_ns[-1] == 525_599 * MINUTE_NS
    assert not bars.missing_minutes_before.any()


@pytest.mark.parametrize(
    ("stamps", "values", "quantities", "error"),
    [
        ([2, 1], [100, 100], [1, 1], "out-of-order"),
        ([1], [float("nan")], [1], "nonfinite"),
        ([1], [100], [0], "strictly positive"),
    ],
)
def test_invalid_batch_rejected(stamps, values, quantities, error) -> None:
    with pytest.raises(ValueError, match=error):
        aggregate_trades(
            np.asarray(stamps, dtype=np.int64),
            np.asarray(values, dtype=np.float64),
            np.asarray(quantities, dtype=np.float64),
        )


def test_execution_and_alpha_timeframes_are_separate() -> None:
    assert TradingCadence().decision_minutes == 60
    assert TradingCadence(decision_minutes=15).execution_minutes == 1
    with pytest.raises(ValueError, match="decision"):
        TradingCadence(decision_minutes=1)
