"""Time-ordering and probabilistic research guardrails."""

import numpy as np
import pandas as pd
import pytest

from lab.market_analysis import MarketAnalysisError
from lab.predictive_research import ForecastSpec, _features, forecast_research


def fake_market(seed=1, periods=950):
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0003, 0.012, periods)
    close = np.exp(np.cumsum(returns)) * 100
    return pd.DataFrame(
        {
            "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": np.full(periods, 1e6),
        },
        index=pd.bdate_range("2021-01-01", periods=periods),
    )


SPEC = ForecastSpec(min_train=160, calibration=45, test=45, horizon=3, coverage=0.9)


def test_no_post_cutoff_lookahead():
    frame = fake_market()
    day = frame.index[780].date()
    before = forecast_research("A", frame, as_of=day, spec=SPEC)
    future = frame.copy()
    future.loc[future.index > pd.Timestamp(day), "close"] = -1
    future.loc[future.index > pd.Timestamp(day), "open"] = np.nan
    after = forecast_research("A", future, as_of=day, spec=SPEC)
    assert before == after
    assert before["qualification"] == "RESEARCH_ONLY_NOT_FOR_TRADING"


def test_horizon_label_does_not_change_feature_at_t():
    frame = fake_market()
    original = _features(frame.iloc[:500])[-1].copy()
    frame.iloc[500, frame.columns.get_loc("close")] *= 5
    assert np.allclose(original, _features(frame.iloc[:500])[-1])


def test_deterministic_results_and_oos_metadata():
    frame = fake_market()
    day = frame.index[850].date()
    before = forecast_research("A", frame, as_of=day, spec=SPEC)
    after = forecast_research("A", frame, as_of=day, spec=SPEC)
    assert before == after
    oos = before["frozen_oos_test"]
    assert oos["train_count"] >= SPEC.min_train
    assert oos["test_count"] == SPEC.test
    assert oos["purge_sessions"] == SPEC.horizon
    assert 0 <= oos["observed_interval_coverage"] <= 1
    assert 0 <= before["as_of_forecast"]["empirical_probability_positive"] <= 1
    assert before["as_of_forecast"]["lower_return_fraction"] <= before["as_of_forecast"]["upper_return_fraction"]
    assert abs(sum(before["volatility_regime"]["transition_probabilities"][0]) - 1) < 1e-12


def test_historical_data_mutation_changes_fingerprint():
    frame = fake_market()
    day = frame.index[780].date()
    original = forecast_research("A", frame, as_of=day, spec=SPEC)
    changed = frame.copy()
    changed.loc[changed.index[700], ["open", "high", "low", "close"]] *= 1.01
    modified = forecast_research("A", changed, as_of=day, spec=SPEC)
    assert original["analysis_id"] != modified["analysis_id"]


def test_incomplete_bars_and_insufficient_history_fail_closed():
    frame = fake_market()
    frame.iloc[680, frame.columns.get_loc("volume")] = np.nan
    with pytest.raises(MarketAnalysisError):
        forecast_research("A", frame, as_of=frame.index[780].date(), spec=SPEC)
    short = fake_market(periods=200)
    with pytest.raises(MarketAnalysisError):
        forecast_research("A", short, as_of=short.index[-1].date(), spec=SPEC)


@pytest.mark.parametrize(
    "bad",
    [
        {"horizon": 0},
        {"horizon": 21},
        {"ridge_alpha": 0},
        {"calibration": 3},
        {"coverage": 1.0},
        {"drift_z_limit": -3},
    ],
)
def test_invalid_hyperparameters_rejected(bad):
    with pytest.raises(MarketAnalysisError):
        ForecastSpec(**bad)


def test_drastic_changed_feature_abstains():
    frame = fake_market()
    row = 800
    for col in ("close", "open", "high", "low"):
        frame.iloc[row, frame.columns.get_loc(col)] *= 5
    result = forecast_research("A", frame, as_of=frame.index[row].date(), spec=SPEC)
    assert result["forecast_status"] == "ABSTAIN_FEATURE_DRIFT"
    assert result["as_of_forecast"]["abstain_on_feature_drift"]
