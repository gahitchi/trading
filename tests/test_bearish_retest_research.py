"""Guarded prospective registration and retrospective research integration tests.

No Binance API requests, real market archives, or protected holdout
are used. Full economics are exercised on synthetic accepted events.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from lab import bearish_retest as br
from lab import bearish_retest_research as rs
from lab import crash_data, crash_funding, crash_study
from lab.research_family import register_family
from lab.trials import history
from tests.test_crash_funding_ledger import one_trade


def configured(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[paths]\ndata="data"\nresults="results"\n')
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "crash_partition.json").write_text(json.dumps({
        "version": 1,
        "holdout_start": "2026-05-08T00:00:00+00:00",
        "history": [],
    }))
    plan = tmp_path / "source-plan.json"
    plan.write_text(json.dumps({
        "version": 2, "kind": crash_funding.KIND,
        "symbol": "BTCUSDT", "start": "2024-01-01",
        "holdout_start": "2026-05-08",
        "pages": [{
            "start_ms": 1704067200000, "end_ms": 1778198399999,
            "rows": 1, "source_sha256": "a" * 64,
        }],
        "event_count": 1, "events_sha256": "b" * 64,
    }))
    return cfg, plan


def mocked_history(monkeypatch, plan):
    k = pd.DataFrame({
        "ts": pd.date_range("2024-01-01T00:01:00Z", periods=3, freq="min"),
        "open": [100.0,100.0,100.0], "high": [101.0,101.0,101.0],
        "low": [99.0,99.0,99.0], "close": [100.0,100.0,100.0],
        "quote_volume": [1.0,1.0,1.0],
    })
    oi = pd.DataFrame({
        "ts": pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="5min"),
        "oi_qty": [2000.0,2001.0], "oi_value": [200000.0,200100.0],
        "oi_valid": [True,True],
    })
    calls = []
    def load(_root, kind, symbol, *, before):
        calls.append((kind, symbol, before))
        assert symbol == "BTCUSDT" and before == rs.BOUNDARY
        return k.copy() if kind == "klines" else oi.copy()
    monkeypatch.setattr(crash_data, "load", load)
    candles = k.set_index("ts")
    monkeypatch.setattr(crash_study, "bars", lambda _k, _oi: candles)
    simulated = br.Result(
        one_trade(), {"breakout": 1, "retest": 1, "confirmation": 1, "trade": 1},
        rs.BOUNDARY,
    )
    monkeypatch.setattr(br, "simulate", lambda _b, *, boundary: simulated)
    source_plan = json.loads(plan.read_text())
    events = [{
        "fundingTime": int(ts.value // 1_000_000),
        "fundingRate": "0.0001",
        "markPrice": "100",
    } for ts in pd.date_range("2024-01-01T08:00:00Z", periods=5, freq="8h")]
    monkeypatch.setattr(
        crash_funding, "load_verified",
        lambda _config, _plan, *, symbol: {
            "plan": source_plan,
            "plan_sha256": crash_funding._digest(source_plan),
            "events": events,
        },
    )
    return calls


def test_proposed_plan_is_exact_frozen_member_and_does_not_read_market(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("market archive was read while proposing plan")
    monkeypatch.setattr(crash_data, "load", forbidden)
    spec = rs.proposed_plan(cfg, plan)
    assert len(spec["members"]) == 1
    m = spec["members"][0]
    assert m["kind"] == "crash-study" and m["instrument"] == rs.KEY
    assert m["window"] == [None, int(rs.BOUNDARY.value) - 1]
    assert m["params"]["funding_events_sha256"] == "b" * 64
    assert m["params"]["max_hold_minutes"] == 240
    assert m["params"]["research_status"] == "retrospective-development-only"
    assert history(tmp_path / "results") == []


def test_evaluation_refuses_missing_registration_before_source_read(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("market archive was read before family registration")
    monkeypatch.setattr(crash_data, "load", forbidden)
    with pytest.raises(ValueError, match="no research family registered"):
        rs.evaluate(cfg, plan)
    assert history(tmp_path / "results") == []


def test_mismatched_registered_member_refused_before_source_read(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    proposal = rs.proposed_plan(cfg, plan)
    proposal["members"][0]["params"]["breach_fraction"] = 0.009
    register_family(tmp_path / "results", proposal)
    def forbidden(*args, **kwargs):
        raise AssertionError("market archive was read after mismatched registration")
    monkeypatch.setattr(crash_data, "load", forbidden)
    with pytest.raises(ValueError, match="not an exact member"):
        rs.evaluate(cfg, plan)
    assert history(tmp_path / "results") == []


def test_registered_evaluation_reconciles_actual_funding_and_records_attempt(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg, plan))
    calls = mocked_history(monkeypatch, plan)
    result = rs.evaluate(cfg, plan)
    assert result["status"] == "RETROSPECTIVE_UNQUALIFIED"
    assert result["qualified"] is False and result["holdout_used"] is False
    assert result["trade_count"] == 1
    assert result["events"]["trade"] == 1
    assert result["funding"]["funded_settlements"] == 2
    assert result["cost_sensitivity"][0]["trades"] == 1
    assert calls == [
        ("klines", "BTCUSDT", rs.BOUNDARY),
        ("oi", "BTCUSDT", rs.BOUNDARY),
    ]
    records = history(tmp_path / "results")
    assert len(records) == 1 and records[0]["status"] == "completed"
    assert records[0]["result"]["qualified"] is False
    assert records[0]["params"] == rs.member(cfg, plan)["params"]
    assert [x["name"] for x in records[0]["inputs"]["streams"]] == [
        "funding:BTCUSDT", "klines:BTCUSDT", "oi:BTCUSDT"
    ]
    assert all(x["complete"] for x in records[0]["inputs"]["streams"])


def test_invalid_oi_source_is_rejected_after_reservation_and_failure_retained(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg, plan))
    calls = mocked_history(monkeypatch, plan)
    # The first call returns good candles; OI source lacks accepted
    # quality provenance, so simulation must never be reached.
    original_load = crash_data.load
    def bad_load(*args, **kwargs):
        df = original_load(*args, **kwargs)
        return df.drop(columns=["oi_valid"]) if args[1] == "oi" else df
    monkeypatch.setattr(crash_data, "load", bad_load)
    monkeypatch.setattr(br, "simulate", lambda *args, **kwargs: pytest.fail("unsafe simulation"))
    with pytest.raises(ValueError, match="accepted OI"):
        rs.evaluate(cfg, plan)
    assert calls == [
        ("klines", "BTCUSDT", rs.BOUNDARY),
        ("oi", "BTCUSDT", rs.BOUNDARY),
    ]
    entries = history(tmp_path / "results")
    assert len(entries) == 1 and entries[0]["status"] == "failed"
    assert entries[0]["result"]["status"] == "unavailable"


def test_changed_holdout_boundary_fails_before_registration_or_data(tmp_path):
    cfg, plan = configured(tmp_path)
    (tmp_path / "results" / "crash_partition.json").write_text(json.dumps({
        "version": 1, "holdout_start": "2026-05-09T00:00:00Z",
    }))
    with pytest.raises(ValueError, match="boundary"):
        rs.proposed_plan(cfg, plan)


def test_explicit_plan_writes_exclusively_and_never_overwrites(tmp_path):
    cfg, plan = configured(tmp_path)
    path = tmp_path / "proposal.json"
    rs._write_exclusive(path, rs.proposed_plan(cfg, plan))
    saved = path.read_text()
    assert json.loads(saved)["members"][0]["instrument"] == rs.KEY
    with pytest.raises(FileExistsError):
        rs._write_exclusive(path, rs.proposed_plan(cfg, plan))
    assert path.read_text() == saved


def test_registered_code_drift_refuses_evaluation_before_market_read(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg, plan))
    real_identity = rs.code_provenance()
    changed = {**real_identity, "python": "different-runtime"}
    monkeypatch.setattr(rs, "code_provenance", lambda: changed)
    monkeypatch.setattr(crash_data, "load",
        lambda *args, **kwargs: pytest.fail("read source after registration mismatch"))
    with pytest.raises(ValueError, match="registered research code/runtime differs"):
        rs.evaluate(cfg, plan)
    assert history(tmp_path / "results") == []


def test_source_plan_change_after_registration_refuses_before_market_read(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg, plan))
    altered = json.loads(plan.read_text())
    altered["events_sha256"] = "c" * 64
    plan.write_text(json.dumps(altered))
    monkeypatch.setattr(crash_data, "load",
        lambda *args, **kwargs: pytest.fail("read market after funding plan mismatch"))
    with pytest.raises(ValueError, match="not an exact member"):
        rs.evaluate(cfg, plan)
    assert history(tmp_path / "results") == []


def test_end_stamped_cutoff_candle_is_excluded_before_signal_or_fingerprint(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg, plan))
    mocked_history(monkeypatch, plan)
    source_load = crash_data.load
    source_bars = crash_study.bars
    def source_with_midnight(root, kind, symbol, *, before):
        frame = source_load(root, kind, symbol, before=before)
        # The last pre-holdout daily archive may label its 23:59
        # candle at exactly midnight on the protected boundary.
        ending = frame.iloc[-1:].copy()
        ending["ts"] = rs.BOUNDARY
        return pd.concat([frame, ending], ignore_index=True)
    monkeypatch.setattr(crash_data, "load", source_with_midnight)
    accepted = []
    def checked_bars(klines, oi):
        assert (klines["ts"] < rs.BOUNDARY).all()
        assert (oi["ts"] < rs.BOUNDARY).all()
        assert len(klines) == 3 and len(oi) == 2
        accepted.append(True)
        return source_bars(klines, oi)
    monkeypatch.setattr(crash_study, "bars", checked_bars)
    output = rs.evaluate(cfg, plan)
    assert accepted and output["trade_count"] == 1
    rows = history(tmp_path / "results")
    assert len(rows) == 1 and rows[0]["status"] == "completed"


def test_archive_row_strictly_past_holdout_boundary_fails_closed_and_is_counted(tmp_path, monkeypatch):
    cfg, plan = configured(tmp_path)
    register_family(tmp_path / "results", rs.proposed_plan(cfg, plan))
    mocked_history(monkeypatch, plan)
    original = crash_data.load
    def invalid_archive(root, kind, symbol, *, before):
        frame = original(root, kind, symbol, before=before)
        if kind == "klines":
            extra = frame.iloc[-1:].copy()
            extra["ts"] = rs.BOUNDARY + pd.Timedelta(minutes=1)
            return pd.concat([frame, extra], ignore_index=True)
        return frame
    monkeypatch.setattr(crash_data, "load", invalid_archive)
    monkeypatch.setattr(br, "simulate", lambda *args, **kwargs: pytest.fail("unsafe simulation"))
    with pytest.raises(ValueError, match="beyond protected boundary"):
        rs.evaluate(cfg, plan)
    rows = history(tmp_path / "results")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["result"]["status"] == "unavailable"


def test_corrected_research_policy_uses_successor_after_failed_prior_generation(tmp_path, monkeypatch):
    from copy import deepcopy

    from lab.research_family import load_family, register_family
    from lab.trials import trial_run
    cfg, plan = configured(tmp_path)
    old_proposal = deepcopy(rs.proposed_plan(cfg, plan))
    old_params = old_proposal["members"][0]["params"]
    old_params["study_policy"] = "guarded-preregistered-funding-adjusted-btc-retest-study-v1"
    old_params["data_policy"] = "source-validated-contract-quantity-oi-veto-minute-bars"
    first = register_family(tmp_path / "results", old_proposal)
    old_member = old_proposal["members"][0]
    with pytest.raises(ValueError, match="boundary mismatch"), trial_run(
        tmp_path / "results", old_member["kind"], old_member["instrument"],
        old_member["params"], tuple(old_member["window"]),
    ):
        raise ValueError("boundary mismatch")
    failures = history(tmp_path / "results")
    assert len(failures) == 1 and failures[0]["status"] == "failed"
    second = register_family(tmp_path / "results", rs.proposed_plan(cfg, plan))
    assert second["previous_registration_sha256"] == first["sha256"]
    assert second["prior_history"] == failures
    mocked_history(monkeypatch, plan)
    report = rs.evaluate(cfg, plan)
    assert report["status"] == "RETROSPECTIVE_UNQUALIFIED"
    rows = history(tmp_path / "results")
    assert len(rows) == 2 and rows[0] == failures[0]
    assert rows[-1]["status"] == "completed"
    assert rows[-1]["family_id"] == second["family_id"]
    assert load_family(tmp_path / "results") == second
