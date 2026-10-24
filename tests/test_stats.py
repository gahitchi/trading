"""Statistics: properties and calibration of PSR, DSR, PBO and the bootstrap CI."""

import math

import numpy as np
import pytest

from lab import stats


def test_psr_matches_closed_form_for_normal_returns():
    sr, t = 0.05, 1000
    expected = stats.NormalDist().cdf(sr * math.sqrt(t - 1) / math.sqrt(1 + sr**2 / 2))
    assert stats.psr(sr, t, 0.0, 3.0) == pytest.approx(expected)


def test_fat_left_tails_lower_confidence():
    assert stats.psr(0.05, 1000, -2.0, 10.0) < stats.psr(0.05, 1000, 0.0, 3.0)


def test_expected_max_sharpe_grows_with_trials():
    v = 0.001
    assert 0 < stats.expected_max_sharpe(10, v) < stats.expected_max_sharpe(100, v) < stats.expected_max_sharpe(1000, v)
    assert stats.expected_max_sharpe(1, v) == 0.0


def test_dsr_controls_false_discoveries_among_many_trials():
    """Best of 50 pure-noise strategies: naive PSR often claims significance, DSR rarely does."""
    rng = np.random.default_rng(0)
    naive = deflated = 0
    reps = 200
    for _ in range(reps):
        trials = rng.normal(0, 0.01, size=(1000, 50))
        srs = trials.mean(0) / trials.std(0, ddof=1)
        best = trials[:, int(np.argmax(srs))]
        d = stats.dsr(best, list(srs))
        naive += d["psr"] > 0.95
        deflated += d["dsr"] > 0.95
    assert naive / reps > 0.5
    assert deflated / reps < 0.10


def test_pbo_flags_pure_noise_and_clears_a_real_edge():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, size=(2000, 12))
    assert 0.3 < stats.pbo(noise)["pbo"] < 0.7
    real = noise.copy()
    real[:, 3] += 0.003  # one configuration has a genuine, persistent edge
    out = stats.pbo(real)
    assert out["pbo"] < 0.05 and out["prob_oos_loss"] < 0.05


def test_sharpe_ci_has_nominal_coverage():
    """A 95% interval should contain the true Sharpe ratio in about 95% of samples."""
    true = 0.0005 / 0.01 * math.sqrt(252)
    hits = 0
    reps = 120
    for s in range(reps):
        r = np.random.default_rng(1000 + s).normal(0.0005, 0.01, 2520)
        lo, hi = stats.sharpe_ci(r, samples=400, seed=s)
        hits += lo < true < hi
    assert 0.88 <= hits / reps <= 0.99


def test_compare_reports_deflated_sharpe_and_counts_trials(goog_lab):
    from lab import experiments
    from lab.signals.registry import SignalSpec

    per = goog_lab.period("train")
    comp, _ = experiments.compare(goog_lab, [SignalSpec("sma_trend")], ["GOOG"], per)
    assert comp.rows[1]["trials"] == 1 and 0 <= comp.rows[1]["dsr"] <= 1
    experiments.compare(goog_lab, [SignalSpec("sma_trend", {"fast": 20}), SignalSpec("rsi_reversion")], ["GOOG"], per)
    comp3, _ = experiments.compare(goog_lab, [SignalSpec("sma_trend")], ["GOOG"], per)  # re-run: not a new trial
    assert comp3.rows[1]["trials"] == 3
    assert comp3.rows[1]["dsr"] <= comp3.rows[1]["psr"]


def test_walk_forward_and_pbo_studies_run(goog_lab):
    from datetime import date

    from lab import data, experiments

    frames = {"GOOG": data.load(goog_lab, "GOOG")}
    specs = experiments.grid_specs("sma_trend", {"fast": [20, 50], "slow": [100, 200]}, goog_lab.research_dir)
    assert len(specs) == 4
    wfa = experiments.walk_forward(
        goog_lab, specs, ["GOOG"], date(2005, 7, 1), date(2011, 12, 31), frames, is_years=3, oos_years=1
    )
    assert len(wfa["windows"]) == 3 and math.isfinite(wfa["oos_active_sharpe"])
    p = experiments.pbo_study(goog_lab, specs, ["GOOG"], goog_lab.period("train"), frames, splits=6)
    assert 0 <= p["pbo"] <= 1 and p["configurations"] == 4


def test_hac_sharpe_penalizes_positive_autocorrelation():
    rng = np.random.default_rng(84)
    e = rng.normal(0, 0.01, 1800)
    returns = np.empty_like(e)
    returns[0] = e[0]
    for i in range(1, len(e)):
        returns[i] = 0.002 + 0.85 * (returns[i - 1] - 0.002) + e[i]
    sr, skew, kurt, n = stats.moments(returns)
    iid_psr = stats.psr(sr, n, skew, kurt)
    report = stats.dsr(returns, [sr])
    assert report["hac_lags"] > 0
    assert report["sharpe_estimator_variance_hac"] > (1 - skew * sr + (kurt - 1) * sr**2 / 4) / (n - 1)
    assert report["psr"] < iid_psr


def test_hac_sharpe_never_understates_the_iid_variance():
    rng = np.random.default_rng(99)
    for _ in range(12):
        returns = rng.normal(0.001, 0.01, 750)
        sr, skew, kurt, n = stats.moments(returns)
        variance, lags = stats.hac_sharpe_variance(returns)
        assert lags >= 1
        assert variance >= (1 - skew * sr + (kurt - 1) / 4 * sr**2) / (n - 1) - 1e-12


def test_nonfinite_hac_inputs_refuse_confidence():
    with pytest.raises(ValueError, match="finite"):
        stats.hac_sharpe_variance(np.array([0.01, float("nan"), 0.02]))
    assert math.isnan(stats.dsr(np.array([0.01, 0.02]), [0.01])["dsr"])
