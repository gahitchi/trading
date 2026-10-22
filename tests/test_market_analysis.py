"""As-of-only multi-asset risk diagnostics and numerical integrity."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from lab.market_analysis import AnalysisSpec, MarketAnalysisError, analyze_market


def frame(seed=7, n=320, scale=1.0):
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0005, 0.013, n)
    c = 100 * np.exp(np.cumsum(r)) * scale
    return pd.DataFrame(
        {
            "open": c * 1.001,
            "high": c * 1.02,
            "low": c * 0.98,
            "close": c,
            "volume": np.full(n, 1e6),
        },
        index=pd.bdate_range("2024-01-01", periods=n),
    )


SPEC = AnalysisSpec(lookback=120, momentum_days=90, skip_recent=20, risk_window=45)


def run(frames, *, cutoff=None):
    day = cutoff or next(iter(frames.values())).index[200].date()
    return analyze_market(frames, as_of=day, spec=SPEC)


def test_future_rows_do_not_change_an_historical_report():
    sample = frame()
    cutoff = sample.index[200].date()
    first = run({"A": sample}, cutoff=cutoff)
    future = sample.copy()
    future.loc[future.index > pd.Timestamp(cutoff), "close"] = 1e9
    future.loc[future.index > pd.Timestamp(cutoff), "open"] = np.nan
    future.loc[future.index > pd.Timestamp(cutoff), "high"] = np.nan
    second = run({"A": future}, cutoff=cutoff)
    assert first == second
    assert first["qualification"] == "EXPLORATORY_ONLY"
    assert len(first["analysis_id"]) == 64


def test_change_to_historical_bar_changes_data_provenance():
    sample = frame()
    first = run({"A": sample})
    edited = sample.copy()
    row = edited.index[150]
    edited.loc[row, ["open", "high", "low", "close"]] *= 1.001
    assert run({"A": edited})["data_sha256"] != first["data_sha256"]


def test_return_scaling_preserves_correlation_and_covariance_is_psd():
    sample = frame()
    result = run({"A": sample, "B": frame(scale=3.0)})
    corr = result["risk"]["correlation"]
    matrix = np.asarray(result["risk"]["annualized_covariance_shrunk_to_diagonal"])
    assert corr[0][1] == pytest.approx(0.85, abs=1e-8)  # 15% diagonal shrinkage
    assert np.allclose(matrix, matrix.T)
    assert np.linalg.eigvalsh(matrix).min() >= -1e-12
    assert result["market"]["breadth_positive_21d"] in (0.0, 1.0)


def test_uncorrelated_independent_assets_remain_finite():
    result = run({"A": frame(seed=2), "B": frame(seed=9)})
    assert -1 <= result["risk"]["correlation"][0][1] <= 1
    assert result["features"]["A"]["median_dollar_volume"] > 0


def test_undefined_correlation_reported_null_not_nan():
    prices = np.full(320, 100.0)
    idx = pd.bdate_range("2024-01-01", periods=320)
    sample = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices, "volume": 1000}, index=idx)
    result = run({"A": sample})
    assert result["risk"]["correlation"] == [[None]]
    assert result["features"]["A"]["realized_vol_annual"] == 0
    assert result["features"]["A"]["historical_expected_shortfall_loss"] == 0
    assert any("Zero-variance" in msg for msg in result["warnings"])


def test_aligned_panel_only_no_fill_forward():
    sample = frame()
    bad = sample.drop(sample.index[190])
    with pytest.raises(MarketAnalysisError, match="unaligned exchange sessions"):
        run({"A": sample, "B": bad})


@pytest.mark.parametrize(
    "damage",
    [
        lambda f: f.assign(close=np.nan),
        lambda f: f.assign(volume=-10),
        lambda f: f.assign(high=1),
        lambda f: f.assign(close=-100),
        lambda f: f.rename(columns={"close": "ask"}),
    ],
)
def test_invalid_market_inputs_are_rejected(damage):
    with pytest.raises(MarketAnalysisError):
        run({"A": damage(frame())})


def test_no_trade_session_is_not_silently_backfilled():
    sample = frame()
    with pytest.raises(MarketAnalysisError, match="no observation"):
        run({"A": sample}, cutoff=date(2026, 1, 1))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookback": 20},
        {"risk_window": 1},
        {"momentum_days": 10, "skip_recent": 10},
        {"covariance_shrinkage": -0.1},
        {"covariance_shrinkage": float("nan")},
        {"tail_level": 1.0},
        {"skip_recent": -1},
    ],
)
def test_invalid_feature_spec_fails(kwargs):
    with pytest.raises(MarketAnalysisError):
        AnalysisSpec(**kwargs)


def test_ticker_input_order_does_not_change_analysis_id():
    x, y = frame(seed=5), frame(seed=6)
    assert run({"A": x, "B": y})["analysis_id"] == run({"B": y, "A": x})["analysis_id"]


def test_ohlcv_after_cutoff_does_not_invalidate_previous_session():
    sample = frame()
    sample.iloc[-1, sample.columns.get_loc("close")] = float("nan")
    assert run({"A": sample})["as_of"] == str(sample.index[200].date())


def test_positive_only_returns_have_zero_loss_expected_shortfall():
    idx = pd.bdate_range("2024-01-01", periods=320)
    close = 100 * np.exp(np.arange(320) * 0.002)
    frame_up = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6},
        index=idx,
    )
    result = run({"UP": frame_up})
    assert result["features"]["UP"]["historical_var_loss"] == 0
    assert result["features"]["UP"]["historical_expected_shortfall_loss"] == 0
