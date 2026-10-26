"""First-principles research: deployable policy binding and crash-durable trial exposure."""

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from lab import experiments
from lab.signals.registry import SignalSpec


def test_research_may_screen_custom_risk_but_deployment_uses_exact_operator_caps(goog_lab, monkeypatch):
    configured, seen = goog_lab.risk, []

    def observe_risk(*args, **kwargs):
        seen.append(kwargs["risk"])
        return SimpleNamespace(metrics={"sharpe": 0})

    monkeypatch.setattr(experiments.backtest, "run", observe_risk)
    spec = SignalSpec("buy_and_hold")
    period = goog_lab.period("train")
    experiments.run_spec(goog_lab, spec, ["GOOG"], {}, period)
    experiments.run_spec(goog_lab, spec, ["GOOG"], {}, period, bound_risk=True)
    assert seen == [experiments.effective_risk(goog_lab, 1), configured]
    assert seen[0].max_drawdown != seen[1].max_drawdown


def test_deployment_and_exploratory_configurations_have_distinct_trial_ids(goog_lab):
    assert experiments.config_fingerprint(goog_lab, bound_risk=True) != experiments.config_fingerprint(goog_lab)


def test_interrupted_candidate_cannot_disappear_from_dsr_denominator(goog_lab, tmp_path):
    cfg = replace(goog_lab, results_dir=tmp_path / "isolated-research")
    spec = SignalSpec("sma_trend", {"fast": 17, "slow": 60})
    label, digest = "train|test", "feed-hash-test"
    experiments.reserve_trial(cfg, label, spec, digest, bound_risk=True)
    with pytest.raises(ValueError, match="unresolved candidate evaluation"):
        experiments.trial_sharpes(cfg, label)
    with pytest.raises(ValueError, match="unresolved candidate evaluation"):
        experiments.effective_trials(cfg)

    # Only the exact completed attempt can resolve its pending reservation.
    active = pd.Series([0.001, -0.002, 0.003], index=pd.bdate_range("2026-01-06", periods=3))
    experiments.register_trial(cfg, label, spec, "completed", active, digest, bound_risk=True)
    assert len(experiments.trial_sharpes(cfg, label)) == 1
    assert experiments.effective_trials(cfg) is None


def test_completed_exploratory_result_does_not_complete_deployment_reservation(goog_lab, tmp_path):
    cfg = replace(goog_lab, results_dir=tmp_path / "separate-policies")
    spec = SignalSpec("sma_trend", {"fast": 9, "slow": 50})
    active = pd.Series([0.001, 0.002, -0.001], index=pd.bdate_range("2026-02-02", periods=3))
    experiments.reserve_trial(cfg, "train", spec, "dataset", bound_risk=True)
    experiments.register_trial(cfg, "train", spec, "exploratory", active, "dataset", bound_risk=False)
    with pytest.raises(ValueError, match="unresolved candidate evaluation"):
        experiments.trial_sharpes(cfg, "train")


def test_null_bootstrap_uses_deployment_risk_for_both_candidate_and_control(goog_lab, monkeypatch):
    seen = []
    frame = pd.DataFrame({"close": [100, 101]}, index=pd.bdate_range("2025-01-02", periods=2))

    def fake_bootstrap(past, seed, return_diagnostics):
        return past, {"fallback_fraction": 0.0, "per_instrument": {}}

    def fake_run(*args, **kwargs):
        seen.append(kwargs["bound_risk"])
        return SimpleNamespace(metrics={"sharpe": 0.0})

    monkeypatch.setattr(experiments.data, "block_bootstrap_panel", fake_bootstrap)
    monkeypatch.setattr(experiments, "run_spec", fake_run)
    experiments._null_worker(
        (goog_lab, SignalSpec("sma_trend"), ["GOOG"], {"GOOG": frame}, goog_lab.period("train"), 1, True)
    )
    assert seen == [True, True]

def test_synthetic_delisting_cannot_pass_research_promotion(goog_lab, monkeypatch):
    """Native-account compatibility bars may remain diagnostic, never qualify alpha."""
    import json

    from lab import factor_stats
    from lab.research import worker

    hdir = goog_lab.research_dir / "hypotheses" / "synthetic_delisting_block"
    hdir.mkdir(parents=True)
    (hdir / "signal.py").write_text("# generated strategy source is mocked here")
    monkeypatch.setattr(worker, "load_config", lambda _: goog_lab)
    monkeypatch.setattr(worker, "available_tickers", lambda _: ["GOOG"])
    monkeypatch.setattr(worker.data, "load_many", lambda *args: {})

    rows = [
        {"delistings_settled": {"GOOG": {"assumed": False}}},
        {"sharpe_edge": 1, "dsr": 1, "signal_errors": 0, "fills": 1},
    ]
    monkeypatch.setattr(experiments, "compare", lambda *args, **kw: (SimpleNamespace(rows=rows), []))
    monkeypatch.setattr(experiments, "null_test", lambda *args, **kw: {"p_value": 0})
    monkeypatch.setattr(factor_stats, "diagnose", lambda *args, **kw: {})

    assert worker.main(["train", str(hdir), "unused"]) == 0
    train = json.loads((hdir / "train.json").read_text())
    assert train["checks"]["no synthetic delisting settlement"] is False
    assert train["passed"] is False
