"""Durable diagnostic replay uses only synthetic data and fixed placebo seeds."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from lab import crash_data, crash_funding, crash_qualification
from lab import crash_postmortem_cli as cli
from lab import crash_study as study
from lab.trials import history
from tests.test_crash_postmortem import minute_bars, original


def _config(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[paths]\ndata="data"\nresults="results"\n')
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": "2026-05-08T00:00:00+00:00"})
    )
    return cfg


def _mocks(monkeypatch, plan_path, *, bad_bars=False):
    bars = minute_bars()
    if bad_bars:
        bars = bars.iloc[:1440].copy()
    frame = original()

    def run(_root, symbols, s, boundary, holdout=False, evidence=None):
        assert symbols == ["BTCUSDT"] and s.mode == "breakdown"
        assert boundary == pd.Timestamp("2026-05-08T00:00:00Z")
        assert not holdout and evidence is not None
        return study.Run(
            trades={"BTCUSDT": frame.copy()},
            baseline={"BTCUSDT": frame.copy()},
            forward={m: [] for m in study.FORWARD_MIN},
            start=pd.Timestamp("2026-01-01T00:01:00Z"),
            end=pd.Timestamp("2026-01-31T23:59:00Z"),
            missing=[],
        )

    monkeypatch.setattr(study, "run", run)

    def load(_root, kind, symbol, *, before):
        assert kind == "klines" and symbol == "BTCUSDT"
        assert before == pd.Timestamp("2026-05-08T00:00:00Z")
        return bars.reset_index(names="ts")

    monkeypatch.setattr(crash_data, "load", load)
    plan = json.loads(plan_path.read_text())
    events = [
        {
            "fundingTime": int(t.value // 1_000_000),
            "fundingRate": "0.0001",
            "markPrice": "100.0",
        }
        for t in pd.date_range("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", freq="8h", inclusive="left")
    ]

    def load_verified(_config, _plan, symbol):
        assert symbol == "BTCUSDT"
        return {
            "plan_sha256": crash_funding._digest(plan),
            "plan": plan,
            "events": events,
        }

    monkeypatch.setattr(crash_funding, "load_verified", load_verified)


def test_postmortem_cli_records_full_fixed_seed_placebo_and_remains_unqualified(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    plan = tmp_path / "funding.json"
    plan.write_text(json.dumps({"events_sha256": "a" * 64, "start": "2026-01-01", "holdout_start": "2026-05-08"}))
    _mocks(monkeypatch, plan)
    out_path = tmp_path / "postmortem.json"
    report = cli.execute(cfg, plan, report_out=out_path)
    assert out_path.is_file()
    assert report["original_trade_count"] == 1
    assert report["matched_placebos"]["n_predeclared_seeds"] == 16
    assert len(report["matched_placebos"]["scenarios"]) == 16
    assert all(x["trades"] == 1 for x in report["matched_placebos"]["scenarios"])
    assert all(x["identical_entry_notional_weights"] for x in report["matched_placebos"]["scenarios"])
    assert report["qualification"]["can_use_protected_holdout"] is False
    assert report["qualification"]["can_claim_statistical_significance"] is False
    assert report["qualification"]["all_historical_attempts"] == 1
    records = history(tmp_path / "results")
    assert len(records) == 1 and records[0]["status"] == "completed"
    assert records[0]["result"]["qualified"] is False
    assert records[0]["result"]["analysis_payload_sha256"] == report["analysis_payload_sha256"]
    assert report["analysis_payload_sha256"] == crash_funding._digest(
        {
            "failure_analysis": report["failure_analysis"],
            "matched_placebos": report["matched_placebos"],
            "per_trade_diagnostics": report["per_trade_diagnostics"],
        }
    )
    assert len(records[0]["params"]["placebo_seeds"]) == 16
    assert records[0]["params"]["placebo_seeds"][0] == 20261105
    assert any(s["name"] == "funding:BTCUSDT" for s in records[0]["inputs"]["streams"])
    assert any(s["name"] == "diagnostic:klines:BTCUSDT" for s in records[0]["inputs"]["streams"])


def test_postmortem_failure_remains_in_attempt_history(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    plan = tmp_path / "funding.json"
    plan.write_text(json.dumps({"events_sha256": "b" * 64, "start": "2026-01-01", "holdout_start": "2026-05-08"}))
    _mocks(monkeypatch, plan, bad_bars=True)
    with pytest.raises(ValueError, match="missing"):
        cli.execute(cfg, plan)
    rows = history(tmp_path / "results")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert crash_qualification.inventory(tmp_path / "results")["all_historical_attempts"] == 1


def test_qualification_never_upgrades_from_descriptive_results(tmp_path):
    _config(tmp_path)
    report = crash_qualification.inventory(tmp_path / "results")
    assert report["status"] == "NOT_QUALIFIED"
    assert report["all_historical_attempts"] == 0
    assert report["distinct_configurations_or_code_versions"] == 0
    assert report["blocked_on"]


def test_changed_holdout_midnight_is_rejected_before_evaluation(tmp_path):
    cfg = _config(tmp_path)
    (tmp_path / "results" / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": "2026-05-08T12:00:00+00:00"})
    )
    plan = tmp_path / "unused-funding.json"
    plan.write_text(json.dumps({"events_sha256": "c" * 64}))
    with pytest.raises(ValueError, match="accepted BTC research boundary"):
        cli.execute(cfg, plan)
    assert history(tmp_path / "results") == []


def test_existing_report_cannot_be_overwritten_or_counted_as_a_new_trial(tmp_path):
    cfg = _config(tmp_path)
    destination = tmp_path / "prior-report.json"
    destination.write_text("protected report")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        cli.execute(cfg, tmp_path / "unused-plan.json", report_out=destination)
    assert destination.read_text() == "protected report"
    assert history(tmp_path / "results") == []
