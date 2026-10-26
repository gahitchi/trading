"""First-principles regression: risk-free excess returns use actual elapsed UTC time."""

import numpy as np
import pandas as pd
import pytest

from lab import metrics


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
@pytest.mark.parametrize(
    ("friday", "monday", "hours"),
    [
        ("2026-03-06 16:00", "2026-03-09 16:00", 71),
        ("2026-10-30 16:00", "2026-11-02 16:00", 73),
    ],
)
def test_compounded_risk_free_accrues_over_real_dst_hours(unit, friday, monday, hours):
    """NY close-to-close crosses different DST offsets; Pandas may store microseconds."""
    idx = pd.DatetimeIndex([friday, monday], tz="America/New_York").as_unit(unit)
    equity = pd.Series([100.0, 100.0], index=idx)
    risk_free = pd.Series([0.05, 0.05], index=idx)
    actual = metrics.excess_returns(equity, risk_free)
    expected = -(1.05 ** ((hours / 24) / 365) - 1)
    assert len(actual) == 1
    assert actual.iloc[0] == pytest.approx(expected, abs=1e-14)


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_intraday_risk_free_period_is_not_rounded_down_to_zero_days(unit):
    idx = pd.DatetimeIndex(["2026-06-03 10:00", "2026-06-03 10:45"], tz="UTC").as_unit(unit)
    equity = pd.Series([100.0, 100.0], index=idx)
    risk_free = pd.Series([0.05, 0.05], index=idx)
    expected = -(1.05 ** ((45 / 60 / 24) / 365) - 1)
    assert metrics.excess_returns(equity, risk_free).iloc[0] == pytest.approx(expected, abs=1e-14)


def test_risk_free_uses_rate_known_at_start_not_future_revision():
    idx = pd.DatetimeIndex(["2026-04-06 20:00Z", "2026-04-07 20:00Z"]).as_unit("us")
    equity = pd.Series([100.0, 100.0], index=idx)
    risk_free = pd.Series([0.02, 0.09], index=idx)
    result = metrics.excess_returns(equity, risk_free)
    assert np.isfinite(result.iloc[0])
    assert result.iloc[0] == pytest.approx(-(1.02 ** (1 / 365) - 1), abs=1e-14)
