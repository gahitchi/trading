"""Correct downside deviation uses all return periods, not only loss days."""

import math

import pandas as pd
import pytest

from lab.metrics import TRADING_DAYS, summarize


def test_sortino_uses_target_downside_rms_including_up_days():
    # Exactly three returns, only one negative: the old implementation
    # returned Sortino=0 because it required two negative observations.
    equity = pd.Series([100.0, 110.0, 99.0, 108.9], index=pd.bdate_range("2024-01-01", periods=4))
    result = summarize(equity)
    returns = pd.Series([0.1, -0.1, 0.1])
    target_rms = math.sqrt((0.0**2 + (-0.1) ** 2 + 0.0**2) / 3)
    expected = (returns.mean() * TRADING_DAYS) / (target_rms * math.sqrt(TRADING_DAYS))
    assert result["sortino"] == pytest.approx(expected, rel=1e-12)


def test_no_down_days_do_not_produce_nan_or_infinity():
    equity = pd.Series([100.0, 101.0, 102.0, 103.0], index=pd.bdate_range("2024-01-01", periods=4))
    assert summarize(equity)["sortino"] == 0.0
