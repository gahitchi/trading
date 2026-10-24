"""Fixes for Gemini's quantitative research audit (docs/audits/2026-10-24-quantitative-research-upgrade-audit.md).
The statistics findings F-R01 to F-R04 are tested in test_stats.py."""

import shutil

import numpy as np
import pandas as pd
import pytest

from lab import data, metrics, provenance

# ----------------------------------------------------------------------------- F-R05


def test_several_marks_a_day_are_annualized_as_one_session():
    days = pd.bdate_range("2024-01-02", periods=300)
    eq = pd.Series(100_000 * np.exp(np.random.default_rng(1).normal(0.0004, 0.01, 300).cumsum()), index=days)
    noon = pd.Series(eq.to_numpy() * 0.999, index=days + pd.Timedelta(hours=12))
    one = metrics.summarize(eq)
    # Each session's last mark (noon here) stands for it; the extra marks do not double the "days".
    two = metrics.summarize(pd.concat([noon, eq]).sort_index())
    assert two["days"] == one["days"] and two["sharpe"] == pytest.approx(one["sharpe"])


def test_sharpe_uses_returns_in_excess_of_the_risk_free_rate():
    days = pd.bdate_range("2024-01-02", periods=252)
    # A book that only earns a 5% T-bill rate has no excess return: Sharpe ~0, not a large positive number.
    eq = pd.Series(100_000 * 1.05 ** (np.arange(252) / 252), index=days)
    eq = eq * (1 + np.random.default_rng(2).normal(0, 1e-4, 252))
    rf = pd.Series(0.05, index=days)
    assert metrics.summarize(eq)["sharpe"] > 5
    excess = metrics.summarize(eq, risk_free=rf)
    assert abs(excess["sharpe"]) < 1.5 and excess["risk_free"] == "series"


def test_risk_free_rates_are_point_in_time(goog_lab):
    data.save_risk_free(
        goog_lab, pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "rate": [5.0, "."]}), source="test"
    )
    s = data.risk_free(goog_lab)
    assert list(s.index) == [pd.Timestamp("2024-01-03")] and s.iloc[0] == pytest.approx(0.05)  # known next day
    with pytest.raises(data.DataError, match="annual percentages"):
        data.save_risk_free(goog_lab, pd.DataFrame({"date": ["2024-01-02"], "rate": [500]}), source="test")


def test_the_risk_free_file_is_part_of_the_trial_identity(goog_lab):
    from lab import experiments

    before = experiments.config_fingerprint(goog_lab)
    data.save_risk_free(goog_lab, pd.DataFrame({"date": ["2024-01-02"], "rate": [5.0]}), source="test")
    assert experiments.config_fingerprint(goog_lab) != before


# ----------------------------------------------------------------------------- F-R16


def test_a_cosmetic_app_edit_keeps_the_simulation_digest(tmp_path, monkeypatch):
    package = tmp_path / "lab"
    shutil.copytree(provenance.__file__.rsplit("/", 1)[0], package, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(provenance, "__file__", str(package / "provenance.py"))
    sim, run = provenance.simulation_digest(), provenance.runtime_digest()
    (package / "app.py").write_text((package / "app.py").read_text() + "\n# cosmetic\n")
    assert provenance.simulation_digest() == sim and provenance.runtime_digest() != run
    (package / "costs.py").write_text((package / "costs.py").read_text() + "\n# changes a result\n")
    assert provenance.simulation_digest() != sim
    (package / "brand_new_module.py").write_text("X = 1\n")  # unlisted modules count as simulation code
    changed = provenance.simulation_digest()
    (package / "brand_new_module.py").write_text("X = 2\n")
    assert provenance.simulation_digest() != changed


# ----------------------------------------------------------------------------- F-R02, F-R04 (workspace level)


def test_near_identical_trials_count_as_about_one(goog_lab):
    from lab import experiments
    from lab.signals.registry import SignalSpec

    per = goog_lab.period("train")
    comp, _ = experiments.compare(
        goog_lab, [SignalSpec("sma_trend", {"fast": 50}), SignalSpec("sma_trend", {"fast": 51})], ["GOOG"], per
    )
    assert len(list((goog_lab.results_dir / "trial_returns").glob("*.npz"))) == 2
    row = comp.rows[1]
    assert row["trials"] == 2 and 1 <= row["effective_trials"] < 1.3  # the two runs are almost the same bet
    experiments.compare(goog_lab, [SignalSpec("rsi_reversion")], ["GOOG"], per)
    # A different strategy adds to the count, but only partly: every active return carries the same
    # "minus buy-and-hold" leg, so even unrelated strategies' active returns are correlated.
    assert row["effective_trials"] + 0.1 < experiments.effective_trials(goog_lab) < 3


def test_the_panel_bootstrap_draws_blocks_of_varying_length():
    prices = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=pd.bdate_range("2015-01-02", periods=600),
    )
    counts = {
        data.block_bootstrap_panel({"X": prices}, seed, return_diagnostics=True)[1]["participating_blocks"]
        for seed in range(8)
    }
    assert len(counts) > 3  # fixed 20-session blocks always gave 30
    assert all(15 < c < 50 for c in counts)
