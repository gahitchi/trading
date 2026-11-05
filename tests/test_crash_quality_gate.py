"""Offline data-quality gate tests: no holdout reads, strategy trials or writes."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.crash_quality_gate import audit_quality, main


def cache(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[paths]\ndata = "data"\nresults = "results"\n')
    results = tmp_path / "results"
    results.mkdir()
    (results / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": "2024-01-03T00:00:00+00:00"})
    )
    root = tmp_path / "data" / "crash"
    candles = root / "klines" / "BTCUSDT"
    interest = root / "oi" / "BTCUSDT"
    candles.mkdir(parents=True)
    interest.mkdir(parents=True)
    for day in ("2024-01-01", "2024-01-02"):
        kt = pd.date_range(day, periods=1440, freq="min", tz="UTC")
        if day == "2024-01-02":
            kt = kt.difference(pd.DatetimeIndex(["2024-01-02T00:30:00Z"]))
        pd.DataFrame({
            "ts": kt,
            "open": np.full(len(kt), 100.0),
            "high": np.full(len(kt), 101.0),
            "low": np.full(len(kt), 99.0),
            "close": np.full(len(kt), 100.0),
            "quote_volume": np.full(len(kt), 500.0),
        }).to_parquet(candles / f"{day}.parquet", index=False)
        ot = pd.date_range(day, periods=288, freq="5min", tz="UTC")
        if day == "2024-01-02":
            ot = ot.difference(pd.DatetimeIndex(["2024-01-02T10:00:00Z"]))
        f = pd.DataFrame({
            "ts": ot,
            "oi_qty": np.full(len(ot), 1000.0),
            "oi_value": np.full(len(ot), 1000000.0),
        })
        if day == "2024-01-02":
            f["oi_valid"] = True
            idx = f.index[f["ts"] == pd.Timestamp("2024-01-02T11:00:00Z")][0]
            f.loc[idx, "oi_qty"] = np.nan
            f.loc[idx, "oi_value"] = 0.0
            f.loc[idx, "oi_valid"] = False
        f.to_parquet(interest / f"{day}.parquet", index=False)

    # Exact protected-file reads must fail, irrespective of file contents.
    (candles / "2024-01-03.parquet").write_bytes(b"PROTECTED")
    (candles / "2024-01.parquet").write_bytes(b"PROTECTED MONTH")
    (interest / "2024-01-03.parquet").write_bytes(b"PROTECTED")
    return cfg, root


def test_quality_report_counts_missing_candles_and_two_separate_oi_exclusion_causes(tmp_path, monkeypatch):
    cfg, _root = cache(tmp_path)
    original = pd.read_parquet

    def guarded(path, *args, **kwargs):
        assert Path(path).name not in ("2024-01-03.parquet", "2024-01.parquet")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", guarded)
    report = audit_quality(cfg)
    assert report["read_only"]
    assert report["scope"] == "pre_holdout_data_quality_only"
    assert report["klines"]["loaded_rows"] == 2879
    assert report["klines"]["grid_minutes"] == 2880
    assert report["klines"]["missing_minutes"] == 1
    assert report["klines"]["missing_ranges_first_12"] == [{
        "start": "2024-01-02T00:30:00+00:00",
        "end": "2024-01-02T00:30:00+00:00",
        "minutes": 1,
    }]
    assert report["oi"]["loaded_rows"] == 575
    assert report["oi"]["quarantined_source_rows"] == 1
    assert report["oi"]["missing_source_events_within_candle_grid"] == 1
    assert report["oi"]["quarantined_events_within_candle_grid"] == 1

    windows = report["potential_decision_windows"]
    assert windows["data_ready_before_quality_veto"] > 0
    assert windows["excluded_by_any_oi_quality_veto"] > 0
    assert windows["excluded_with_missing_source_in_lookback"] > 0
    assert windows["excluded_with_quarantine_in_lookback"] > 0
    assert windows["data_ready_before_quality_veto"] == (
        windows["data_ready_after_quality_veto"]
        + windows["excluded_by_any_oi_quality_veto"]
    )
    assert report["assumed_taker_round_trip_cost_bps"] == {
        "fee_5_slippage_5_each_side": 20,
        "fee_5_slippage_10_each_side": 30,
        "fee_5_slippage_20_each_side": 50,
    }
    assert list((tmp_path / "results").iterdir()) == [
        tmp_path / "results" / "crash_partition.json"
    ]


def test_report_is_read_only_and_reproducible(tmp_path):
    cfg, root = cache(tmp_path)
    files = list(root.rglob("*.parquet"))
    before = {str(p): p.read_bytes() for p in files}
    first = audit_quality(cfg, window_min=60)
    again = audit_quality(cfg, window_min=60)
    assert first == again
    assert before == {str(p): p.read_bytes() for p in files}


def test_candle_off_minute_grid_fails_closed(tmp_path):
    cfg, root = cache(tmp_path)
    target = root / "klines" / "BTCUSDT" / "2024-01-01.parquet"
    df = pd.read_parquet(target)
    df.loc[15, "ts"] += pd.Timedelta(seconds=30)
    df.to_parquet(target, index=False)
    with pytest.raises(ValueError, match="off-grid"):
        audit_quality(cfg)


def test_invalid_ohlc_fails_closed(tmp_path):
    cfg, root = cache(tmp_path)
    target = root / "klines" / "BTCUSDT" / "2024-01-01.parquet"
    df = pd.read_parquet(target)
    df.loc[15, "high"] = 5
    df.to_parquet(target, index=False)
    with pytest.raises(ValueError, match="invalid candle OHLC"):
        audit_quality(cfg)


def test_cli_read_only_output_and_window_bounds(tmp_path, capsys):
    cfg, _ = cache(tmp_path)
    assert main(["--config", str(cfg), "--window-min", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["window_min"] == 1
    assert report["read_only"] is True
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(cfg), "--window-min", "0"])
    assert exc.value.code == 2
    assert "between 1 and 240" in capsys.readouterr().err


def test_reject_symlinks_before_reading_caches(tmp_path):
    cfg, root = cache(tmp_path)
    alias = root / "klines" / "BTCUSDT" / "2024-01-01.parquet"
    alias.unlink()
    alias.symlink_to(root / "klines" / "BTCUSDT" / "2024-01-03.parquet")
    with pytest.raises(ValueError, match="unsafe symlink"):
        audit_quality(cfg)


def test_missing_input_is_not_reported_as_success(tmp_path):
    cfg, root = cache(tmp_path)
    for p in (root / "oi" / "BTCUSDT").glob("2024-01-0[12].parquet"):
        p.unlink()
    with pytest.raises(ValueError, match="requires pre-holdout"):
        audit_quality(cfg)
