"""Unattended PAPER authority requires independently reviewed, source-bound evidence."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lab import data, experiments
from lab.authority import Authority
from lab.cli import build_parser
from lab.persistence import atomic_json, read_json
from lab.research import builtin_qualification as qualification
from lab.research.approval import ApprovalError
from lab.signals.registry import SignalSpec

GOOD_SOURCE = {
    "survivorship_free": True,
    "point_in_time_universe": True,
    "adjusted": True,
    "split_history": True,
    "dropped_incomplete": 0,
    "sources": ["verified-vendor"],
    "shorting": False,
    "borrow_model": "historical_close_book",
}


def verified_environment(cfg, monkeypatch):
    frames = {"GOOG": data.gbm(100, 17)}
    monkeypatch.setattr(qualification.data, "load_many", lambda *_: frames)
    monkeypatch.setattr(qualification.data, "quality", lambda *_: GOOD_SOURCE)
    return frames


def test_autonomous_mandate_requires_actual_deployment_evidence(tmp_path):
    ident = {"tickers": ["GOOG"], "deployment": {"risk": "balanced"}, "autonomous_qualified": False}
    authority = Authority(tmp_path, ident)
    with pytest.raises(ValueError, match="independently qualified"):
        authority.issue("autonomous")
    authority.issue("supervised")
    assert authority.read()["mode"] == "supervised"
    authority.issue("manual")
    assert authority.read()["mode"] == "manual"


def test_autonomous_mandate_cannot_be_restarted_under_unqualified_identity(tmp_path):
    source = tmp_path / "approved.json"
    source.write_text('{"approved":true}')
    qualified = {
        "tickers": ["GOOG"],
        "deployment": {"risk": "balanced"},
        "autonomous_qualified": True,
        "autonomous_evidence_path": str(source),
        "autonomous_evidence_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    authority = Authority(tmp_path, qualified)
    authority.issue("autonomous")
    assert authority.read()["mode"] == "autonomous"
    source.write_text('{"approved":false}')
    with pytest.raises(ValueError, match="approval evidence changed"):
        authority.read()
    source.unlink()
    with pytest.raises(ValueError, match="evidence is unavailable"):
        authority.read()
    invalid = {**qualified, "autonomous_qualified": False}
    with pytest.raises(ValueError, match="independently qualified"):
        Authority(tmp_path, invalid).read()


def test_builtin_train_test_bound_to_actual_paper_risk_and_data(goog_lab, monkeypatch):
    verified_environment(goog_lab, monkeypatch)
    spec = SignalSpec("sma_trend")
    rows = [
        {},
        {"sharpe_edge": 2.5, "dsr": 0.99, "signal_errors": 0, "fills": 20},
    ]
    seen = []

    def compare(cfg, specs, tickers, period, frames, **kwargs):
        seen.append((period.name, kwargs.get("bound_risk"), cfg.capital))
        return SimpleNamespace(rows=rows), []

    monkeypatch.setattr(experiments, "compare", compare)
    monkeypatch.setattr(experiments, "null_test", lambda *args, **kwargs: {"p_value": 0.001})
    evidence = qualification.evaluate_builtin(goog_lab, spec, ["GOOG"])
    assert evidence["qualified"] is True
    assert seen == [
        ("train", True, goog_lab.paper.capital),
        ("test", True, goog_lab.paper.capital),
    ]
    assert evidence["identity"]["deployment"]["risk"]["max_weight"] == goog_lab.risk.max_weight
    assert evidence["identity"]["research_fingerprint"]


def test_failed_training_never_exposes_test_period(goog_lab, monkeypatch):
    verified_environment(goog_lab, monkeypatch)
    observed = []

    def compare(cfg, specs, tickers, period, frames, **kwargs):
        observed.append(period.name)
        return SimpleNamespace(rows=[{}, {"sharpe_edge": -1.0, "dsr": 0.01, "signal_errors": 0, "fills": 10}]), []

    monkeypatch.setattr(experiments, "compare", compare)
    monkeypatch.setattr(experiments, "null_test", lambda *args, **kwargs: {"p_value": 1.0})
    outcome = qualification.evaluate_builtin(goog_lab, SignalSpec("sma_trend"), ["GOOG"])
    assert outcome["qualified"] is False and outcome["test"] is None
    assert observed == ["train"]


def test_verifiable_builtin_approval_invalidates_data_and_parameter_changes(goog_lab, monkeypatch):
    verified_environment(goog_lab, monkeypatch)
    spec = SignalSpec("sma_trend")
    frames = data.gbm(100, 21)
    monkeypatch.setattr(qualification.data, "load_many", lambda *_: {"GOOG": frames})
    good = {
        "identity": qualification._identity(goog_lab, spec, ["GOOG"], {"GOOG": frames}),
        "train": {
            "candidate": {"sharpe_edge": 2.0, "dsr": 0.99, "signal_errors": 0, "fills": 10},
            "null": {"p_value": 0.001},
            "passed": True,
        },
        "test": {"candidate": {"sharpe_edge": 0.2, "signal_errors": 0, "fills": 3}, "survived": True},
        "qualified": True,
    }
    file = qualification.approve_builtin(goog_lab, spec, ["GOOG"], good)
    assert file.exists()
    assert len(qualification.verify_builtin(goog_lab, spec, ["GOOG"])) == 64
    with pytest.raises(ApprovalError, match="missing built-in"):
        qualification.verify_builtin(goog_lab, replace(spec, params={"fast": 7}), ["GOOG"])
    compromised = read_json(file)
    compromised["train"]["candidate"]["dsr"] = -3.0
    atomic_json(file, compromised)
    with pytest.raises(ApprovalError, match="training metrics"):
        qualification.verify_builtin(goog_lab, spec, ["GOOG"])
    atomic_json(file, good | {"reviewed": True})
    monkeypatch.setattr(qualification.data, "load_many", lambda *_: {"GOOG": data.gbm(100, 33)})
    with pytest.raises(ApprovalError, match="missing built-in"):
        qualification.verify_builtin(goog_lab, spec, ["GOOG"])


def test_policy_change_preserves_prior_manifest_and_requires_separate_approval(goog_lab, monkeypatch):
    verified_environment(goog_lab, monkeypatch)
    spec = SignalSpec("sma_trend")
    previous = qualification.evidence_path(goog_lab, spec, ["GOOG"])
    another_risk = replace(goog_lab.risk, max_drawdown=goog_lab.risk.max_drawdown / 2)
    variant = replace(goog_lab, risk=another_risk)
    current = qualification.evidence_path(variant, spec, ["GOOG"])
    assert previous != current
    assert previous.parent == current.parent
    with pytest.raises(ApprovalError, match="missing built-in"):
        qualification.verify_builtin(variant, spec, ["GOOG"])


def test_unofficial_or_survivor_only_data_never_qualifies(goog_lab, monkeypatch):
    verified_environment(goog_lab, monkeypatch)
    spec = SignalSpec("sma_trend")
    monkeypatch.setattr(qualification.data, "quality", lambda *_: {**GOOD_SOURCE, "sources": ["yahoo_finance"]})
    with pytest.raises(ApprovalError, match="unofficial"):
        qualification.evaluate_builtin(goog_lab, spec, ["GOOG"])
    monkeypatch.setattr(qualification.data, "quality", lambda *_: {**GOOD_SOURCE, "point_in_time_universe": False})
    with pytest.raises(ApprovalError, match="point-in-time"):
        qualification.evaluate_builtin(goog_lab, spec, ["GOOG"])


def test_cli_exposes_explicit_builtin_qualification_workflow():
    parsed = build_parser().parse_args(
        ["research", "qualify-builtin", "sma_trend", "--tickers", "GOOG", "--set", "sma_trend.short=1"]
    )
    assert parsed.signal == "sma_trend" and parsed.tickers == ["GOOG"]
    assert parsed.fn.__name__ == "cmd_research_qualify_builtin"
