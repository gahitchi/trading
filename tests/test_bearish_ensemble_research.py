"""Synthetic-only registration and guarded evaluation tests for bearish ensemble."""

import json
from pathlib import Path

import pandas as pd
import pytest

from lab import bearish_ensemble_research as rs
from lab import crash_data, crash_study
from lab.research_family import register_family
from lab.trials import history
from tests.test_bearish_ensemble_screen import history as sample_history


def configured(tmp_path: Path) -> Path:
    config = tmp_path / "lab.toml"
    config.write_text('[paths]\ndata="data"\nresults="results"\n')
    results = tmp_path / "results"
    results.mkdir()
    (results / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": rs.BOUNDARY.isoformat(), "history": []})
    )
    return config


def fake_archives(monkeypatch, b):
    calls = []
    k = b.drop(columns=["oi_invalid"]).reset_index(names="ts")
    oi = pd.DataFrame({
        "ts": b.index[::5],
        "oi_qty": [100.0] * len(b.index[::5]),
        "oi_value": [10000.0] * len(b.index[::5]),
    })

    def load(_root, kind, symbol, *, before):
        calls.append((kind, symbol, before))
        assert symbol == "BTCUSDT" and before == rs.BOUNDARY
        return k.copy() if kind == "klines" else oi.copy()

    monkeypatch.setattr(crash_data, "load", load)
    grid = b.copy()
    grid["oi"] = 100.0
    monkeypatch.setattr(crash_study, "bars", lambda _k, _oi: grid.copy())
    return calls


def test_plan_is_frozen_without_archive_access(tmp_path, monkeypatch):
    cfg = configured(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("market archive accessed before registration")

    monkeypatch.setattr(crash_data, "load", forbidden)
    plan = rs.proposed_plan(cfg)
    m = plan["members"][0]
    assert m["params"]["signal"]["fast_ema"] == 20
    assert m["params"]["side"] == -1
    assert m["window"] == [None, int(rs.BOUNDARY.value) - 1]
    assert m["params"]["research_status"] == "retrospective-unqualified"
    assert history(tmp_path / "results") == []


def test_evaluation_without_registration_refused_before_data_access(tmp_path, monkeypatch):
    cfg = configured(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("market archive accessed before registration")

    monkeypatch.setattr(crash_data, "load", forbidden)
    with pytest.raises(ValueError, match="no research family registered"):
        rs.evaluate(cfg)
    assert history(tmp_path / "results") == []


def test_mismatched_plan_refuses_evaluation_without_attempt(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    wrong = rs.proposed_plan(cfg)
    wrong["members"][0]["params"]["signal"]["momentum_min_bps"] = 24.0
    register_family(tmp_path / "results", wrong)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("market archive accessed on mismatched plan")

    monkeypatch.setattr(crash_data, "load", forbidden)
    with pytest.raises(ValueError, match="not an exact member"):
        rs.evaluate(cfg)
    assert history(tmp_path / "results") == []


def test_exact_registered_research_writes_audited_replay(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg))
    calls = fake_archives(monkeypatch, sample_history(240))
    report = rs.evaluate(cfg)
    assert calls == [("klines", "BTCUSDT", rs.BOUNDARY), ("oi", "BTCUSDT", rs.BOUNDARY)]
    assert report["status"] == "RETROSPECTIVE_UNQUALIFIED"
    assert report["qualified"] is False and report["holdout_used"] is False
    assert report["account_equity_modeled"] is False
    assert report["signals"] == 1
    assert report["trades"] == 1
    assert report["sum_net_trade_return_not_portfolio"] is not None
    attempts = history(tmp_path / "results")
    assert len(attempts) == 1 and attempts[0]["status"] == "completed"
    assert len(attempts[0]["inputs"]["streams"]) == 3


def test_source_error_is_recorded_after_attempt_reservation(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg))
    calls = []

    def fail(_root, kind, _symbol, *, before):
        assert before == rs.BOUNDARY
        calls.append(kind)
        raise ValueError("no archived observations")

    monkeypatch.setattr(crash_data, "load", fail)
    with pytest.raises(ValueError, match="no archived observations"):
        rs.evaluate(cfg)
    assert calls == ["klines"]
    attempts = history(tmp_path / "results")
    assert len(attempts) == 1 and attempts[0]["status"] == "failed"
    assert attempts[0]["result"]["status"] == "unavailable"


def test_replay_open_inventory_returns_no_pnl_summary(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg))
    b = sample_history(240)
    b.loc[b.index[203], "oi_invalid"] = True
    fake_archives(monkeypatch, b)
    report = rs.evaluate(cfg)
    assert report["status"] == "UNAVAILABLE_OPEN_INVENTORY"
    assert report["halted"] is True
    assert "mean_net_trade_bps" not in report
    assert "sum_net_trade_return_not_portfolio" not in report


def test_missing_or_altered_partition_fails_before_market_reads(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    wrong = tmp_path / "results" / "crash_partition.json"
    wrong.write_text(json.dumps({"version": 1, "holdout_start": "2026-05-09T00:00:00Z"}))
    monkeypatch.setattr(crash_data, "load", lambda *_a, **_k: pytest.fail("unexpected market read"))
    with pytest.raises(ValueError, match="boundary"):
        rs.proposed_plan(cfg)


def test_plan_out_does_not_overwrite_existing_file(tmp_path):
    cfg = configured(tmp_path)
    dest = tmp_path / "proposal.json"
    rs._write_exclusive(dest, rs.proposed_plan(cfg))
    with pytest.raises(FileExistsError):
        rs._write_exclusive(dest, rs.proposed_plan(cfg))
