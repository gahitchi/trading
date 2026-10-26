"""Crash/tamper/nonrepeatability tests for candidate evidence used in selection correction."""

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lab import experiments
from lab.cli import build_parser, cmd_research_trials
from lab.signals.registry import SignalSpec


def _trial(tmp_path, goog_lab):
    cfg = replace(goog_lab, results_dir=tmp_path / "isolated-trials")
    spec = SignalSpec("sma_trend", {"fast": 17, "slow": 60})
    returns = pd.Series([0.001, 0.003, -0.002, 0.005], index=pd.bdate_range("2026-02-02", periods=4))
    return cfg, spec, returns


def test_complete_trial_is_checksum_bound_and_visible_through_cli(tmp_path, goog_lab, capsys):
    cfg, spec, active = _trial(tmp_path, goog_lab)
    experiments.reserve_trial(cfg, "train", spec, "vendor-input")
    experiments.register_trial(cfg, "train", spec, "tested", active, "vendor-input")
    report = experiments.trial_registry_status(cfg)
    assert report["qualification_allowed"] and report["events"] == 2
    assert report["candidates"] == report["verified"] == 1 and not report["issues"]
    assert len(experiments.trial_sharpes(cfg, "train")) == 1

    assert build_parser().parse_args(["research", "trials", "--json"]).fn is cmd_research_trials
    assert cmd_research_trials(cfg, SimpleNamespace(json=True)) == 0
    assert '"qualification_allowed": true' in capsys.readouterr().out


def test_missing_completed_npz_blocks_significance(tmp_path, goog_lab):
    cfg, spec, active = _trial(tmp_path, goog_lab)
    experiments.register_trial(cfg, "train", spec, "tested", active)
    registry = experiments._trial_events(cfg)
    completed = registry[-1]
    assert len(completed["returns_sha256"]) == 64
    experiments._trial_returns_path(cfg, completed["fingerprint"]).unlink()
    report = experiments.trial_registry_status(cfg)
    assert not report["qualification_allowed"]
    assert "missing completed" in report["issues"][0]["reason"]
    with pytest.raises(ValueError, match="missing completed return stream"):
        experiments.effective_trials(cfg)


def test_corrupted_npz_blocks_significance_even_if_sharpe_is_present(tmp_path, goog_lab):
    cfg, spec, active = _trial(tmp_path, goog_lab)
    experiments.register_trial(cfg, "train", spec, "tested", active)
    completed = experiments._trial_events(cfg)[-1]
    path = experiments._trial_returns_path(cfg, completed["fingerprint"])
    path.write_bytes(path.read_bytes() + b"tampering")
    with pytest.raises(ValueError, match="digest mismatch"):
        experiments.trial_sharpes(cfg, "train")
    assert experiments.trial_registry_status(cfg)["blocked"] == 1


def test_repeated_identical_attempt_preserves_the_original_sha(tmp_path, goog_lab):
    cfg, spec, active = _trial(tmp_path, goog_lab)
    experiments.register_trial(cfg, "train", spec, "first", active)
    initial = experiments._trial_events(cfg)[-1]
    experiments.register_trial(cfg, "train", spec, "second", active)
    events = experiments._trial_events(cfg)
    assert len(events) == 2 and events[0]["returns_sha256"] == events[-1]["returns_sha256"]
    assert experiments.trial_registry_status(cfg)["verified"] == 1


def test_divergent_retry_is_quarantined_permanently_even_if_retried_again(tmp_path, goog_lab):
    cfg, spec, active = _trial(tmp_path, goog_lab)
    experiments.register_trial(cfg, "train", spec, "first", active)
    original = experiments._trial_events(cfg)[-1]
    path = experiments._trial_returns_path(cfg, original["fingerprint"])
    initial_bytes = path.read_bytes()

    with pytest.raises(ValueError, match="nonrepeatable"):
        experiments.register_trial(cfg, "train", spec, "second", active * 2)
    assert path.read_bytes() == initial_bytes
    assert experiments._trial_events(cfg)[-1]["status"] == "conflict"
    # A later *identical* completion cannot erase the anomalous attempt from the protocol.
    experiments.register_trial(cfg, "train", spec, "third", active)
    with pytest.raises(ValueError, match="quarantined"):
        experiments.trial_sharpes(cfg, "train")
    assert not experiments.trial_registry_status(cfg)["qualification_allowed"]


def test_crash_before_npz_commit_leaves_an_unresolved_attempt(tmp_path, goog_lab, monkeypatch):
    cfg, spec, active = _trial(tmp_path, goog_lab)
    experiments.reserve_trial(cfg, "train", spec, "source")
    original_save = np.savez_compressed

    def crash(stream, **kwargs):
        stream.write(b"incomplete")
        raise RuntimeError("crash during npz encoding")

    monkeypatch.setattr(np, "savez_compressed", crash)
    with pytest.raises(RuntimeError, match="crash"):
        experiments.register_trial(cfg, "train", spec, "interrupted", active, "source")
    assert not list((cfg.results_dir / "trial_returns").glob("*.npz"))
    assert experiments.trial_registry_status(cfg)["blocked"] == 1
    monkeypatch.setattr(np, "savez_compressed", original_save)
    experiments.register_trial(cfg, "train", spec, "replayed", active, "source")
    assert experiments.trial_registry_status(cfg)["qualification_allowed"]


def test_cli_reports_interrupted_trial_without_changing_any_data(tmp_path, goog_lab, capsys):
    cfg, spec, _ = _trial(tmp_path, goog_lab)
    experiments.reserve_trial(cfg, "train", spec, "source")
    before = experiments._trials_path(cfg).read_bytes()
    assert cmd_research_trials(cfg, SimpleNamespace(json=False)) == 1
    assert "unresolved candidate evaluation" in capsys.readouterr().out
    assert experiments._trials_path(cfg).read_bytes() == before
