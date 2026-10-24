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


# ----------------------------------------------------------------- Gemini audit, pillar 1 (F-R01 to F-R04)


def _ar1(rng, n, phi, sd=0.01):
    e = rng.normal(0, sd, n)
    x = np.empty(n)
    x[0] = e[0]
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def test_sharpe_variance_reduces_to_the_iid_formula_without_lags():
    r = np.random.default_rng(3).standard_t(5, 3000) * 0.01 + 0.0004
    mu, sd = r.mean(), r.std()
    z = (r - mu) / sd
    sr, skew, kurt = mu / sd, np.mean(z**3), np.mean(z**4)
    assert stats.sharpe_variance(r, lags=0) == pytest.approx(1 - skew * sr + (kurt - 1) / 4 * sr**2)


def test_psr_accounts_for_autocorrelation():
    """F-R01: zero-mean AR(1) returns (phi 0.5). The i.i.d. variance claimed significance in 15% of samples at
    the 95% level; the HAC variance brings that close to the nominal 5%."""
    rng = np.random.default_rng(5)
    reps = 400
    false = sum(stats.dsr(_ar1(rng, 1000, 0.5), [0.0])["psr"] > 0.95 for _ in range(reps))
    assert false / reps < 0.10
    ar = _ar1(np.random.default_rng(6), 2000, 0.5)
    assert stats.sharpe_variance(ar) > 1.5 * stats.sharpe_variance(ar, lags=0)


def test_one_degenerate_trial_cannot_inflate_the_hurdle():
    """F-R02: one broken trial at -4 annual Sharpe raised SR0 from 0.87 to 1.84 annual; winsorized, to 1.09."""
    rng = np.random.default_rng(5)
    trials = list(rng.normal(0, 0.03, 20))
    r = rng.normal(0, 0.01, 1000)
    clean = stats.dsr(r, trials)["expected_max_sharpe_annual"]
    dirty = stats.dsr(r, [*trials, -0.25])["expected_max_sharpe_annual"]
    assert clean < dirty < 1.3 * clean
    assert stats.robust_variance([0.01, 0.02, 0.03]) == pytest.approx(np.var([0.01, 0.02, 0.03], ddof=1))


def test_correlated_trials_count_as_fewer_independent_trials():
    """F-R02: N near-identical runs are one trial, not N."""
    assert stats.effective_trials(10, 0.95) == pytest.approx(1.45)
    assert stats.effective_trials(10, 0.0) == 10 and stats.effective_trials(10, -0.3) == 10
    r = np.random.default_rng(8).normal(0.0005, 0.01, 1500)
    trials = list(np.random.default_rng(9).normal(0, 0.02, 10))
    independent = stats.dsr(r, trials)
    twins = stats.dsr(r, trials, n_effective=stats.effective_trials(10, 0.95))
    assert twins["effective_trials"] == pytest.approx(1.45) and twins["dsr"] > independent["dsr"]
    assert stats.dsr(r, trials, n_effective=50)["effective_trials"] == 10  # never more than the trials run


def test_pbo_purges_and_embargoes_the_boundaries():
    """F-R03: persistent (AR 0.98) noise configurations look less overfit than they are when the
    out-of-sample ranking starts right at an in-sample boundary; purging moves PBO back toward 0.5."""

    def mean_pbo(**kw):
        out = []
        for s in range(15):
            g = np.random.default_rng(100 + s)
            m = np.column_stack([_ar1(g, 2000, 0.98) for _ in range(10)])
            out.append(stats.pbo(m, splits=10, **kw)["pbo"])
        return np.mean(out)

    plain, purged = mean_pbo(), mean_pbo(purge=50, embargo=0.01)
    assert plain < purged and abs(purged - 0.5) < abs(plain - 0.5)
    noise = np.random.default_rng(1).normal(0, 0.01, size=(400, 4))
    assert stats.pbo(noise, splits=4, purge=10)["purge"] == 10
    with pytest.raises(ValueError, match="fewer than 2"):
        stats.pbo(noise, splits=4, purge=60)


def test_stationary_bootstrap_never_wraps_the_end_onto_the_start():
    """F-R04: blocks restart at a random point instead of stitching the last date onto the first."""
    idx = stats.stationary_indices(300, 20.0, 400, np.random.default_rng(2))
    after_end = idx[:, 1:][idx[:, :-1] == 299]
    assert len(after_end) > 100 and (after_end == 0).mean() < 0.05  # a fresh random start, not a wrap to 0
    steps = np.diff(idx, axis=1) == 1
    assert 15 < 1 / (1 - steps.mean()) < 25  # geometric blocks with the requested mean length


def test_block_length_follows_the_dependence():
    rng = np.random.default_rng(4)
    assert stats.optimal_block_length(rng.normal(size=2000)) < 3
    assert stats.optimal_block_length(_ar1(rng, 2000, 0.9)) > 10
