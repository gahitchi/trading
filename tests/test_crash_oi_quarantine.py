"""Explicit, source-hash-pinned OI exceptions must never become trading signals."""

import hashlib

import numpy as np
import pandas as pd
import pytest

from lab import crash_data
from lab.crash_oi_audit import audit_migrations
from lab.crash_oi_migration import migrate_batch
from lab.crash_oi_quarantine import quarantine_day
from lab.crash_study import CrashSettings, bars, signals
from tests.test_crash_oi_migration import DATA, HEADER, SECOND, setup_cache, zipped


def anomaly(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    source = folder / "2024-01-01.parquet"
    old = pd.read_parquet(source)
    old.loc[1, "oi_value"] = 0.0
    old.to_parquet(source, index=False)
    blob = zipped(HEADER + DATA.replace(",105,3990000", ",0,0"))
    args = {
        "config_path": cfg,
        "symbol": "BTCUSDT",
        "day": "2024-01-01",
        "archive_sha256": hashlib.sha256(blob).hexdigest(),
        "legacy_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expect_zero_pairs": 1,
        "get": lambda _: blob,
    }
    return cfg, folder, blob, args


def test_default_parser_does_not_accept_original_binance_zero_pairs(tmp_path):
    _, _, blob, _ = anomaly(tmp_path)
    with pytest.raises(ValueError, match="must be positive"):
        crash_data.parse_metrics(blob)
    q = crash_data.parse_metrics(blob, quarantine_zero_pairs=True)
    assert list(q.columns) == ["ts", "oi_qty", "oi_value", "oi_valid"]
    assert q["oi_valid"].tolist() == [True, False]
    assert np.isnan(q["oi_qty"].iloc[1])
    assert q["oi_value"].iloc[1] == 0.0


def test_quarantine_dry_run_is_read_only_and_bound_to_both_hashes(tmp_path):
    _, folder, _, args = anomaly(tmp_path)
    before = (folder / "2024-01-01.parquet").read_bytes()
    report = quarantine_day(**args)
    assert report["read_only"] and report["dry_run"]
    assert report["eligible_for_explicit_quarantine"]
    assert report["quarantine_zero_pairs"] == 1
    assert before == (folder / "2024-01-01.parquet").read_bytes()
    assert not (folder / ".oi-quantity-migration.lock").exists()
    assert not (tmp_path / "data" / "crash" / "oi-original-backups").exists()
    with pytest.raises(ValueError, match="original ZIP SHA-256 differs"):
        quarantine_day(**{**args, "archive_sha256": "0" * 64})
    with pytest.raises(ValueError, match="legacy Parquet SHA-256 changed"):
        quarantine_day(**{**args, "legacy_sha256": "0" * 64})


def test_failed_normal_migration_then_quarantine_retains_original_backup_and_audit(tmp_path):
    cfg, folder, blob, args = anomaly(tmp_path)
    source = folder / "2024-01-01.parquet"
    original = source.read_bytes()
    with pytest.raises(ValueError, match="stopped at 2024-01-01.parquet"):
        migrate_batch(cfg, limit=1, apply=True, get=lambda _: blob)
    backup = (
        tmp_path / "data" / "crash" / "oi-original-backups"
        / "BTCUSDT" / "2024-01-01.parquet"
    )
    assert backup.read_bytes() == original
    assert not audit_migrations(cfg)["ok"]  # orphaned backup from failed attempt

    done = quarantine_day(**args, apply=True)
    assert done["result"] == "migrated"
    assert backup.read_bytes() == original
    upgraded = pd.read_parquet(source)
    assert pd.isna(upgraded["oi_qty"].iloc[1])
    assert upgraded["oi_valid"].tolist() == [True, False]
    assert upgraded["oi_value"].tolist() == [4200000.0, 0.0]
    assert crash_data.legacy_oi_paths(
        tmp_path / "data" / "crash", "BTCUSDT", pd.Timestamp("2024-01-03").date()
    ) == []
    checked = audit_migrations(cfg)
    assert checked["ok"], checked["issues"]
    assert checked["verified_files"] == 1
    assert checked["verified_records"] == 2
    assert checked["legacy_remaining"] == 0


def test_quarantine_refuses_mismatched_pair_even_with_pinned_zip(tmp_path):
    _, folder, _, args = anomaly(tmp_path)
    bad = zipped(HEADER + DATA.replace(",105,3990000", ",105,0"))
    args["get"] = lambda _: bad
    args["archive_sha256"] = hashlib.sha256(bad).hexdigest()
    old = (folder / "2024-01-01.parquet").read_bytes()
    with pytest.raises(ValueError, match="unexpected number|must be positive"):
        quarantine_day(**args)
    assert (folder / "2024-01-01.parquet").read_bytes() == old


def test_quality_mask_cannot_hide_positive_or_unrelated_missing_oi(tmp_path):
    cfg, folder, _, args = anomaly(tmp_path)
    quarantine_day(**args, apply=True)
    file = folder / "2024-01-01.parquet"
    frame = pd.read_parquet(file)
    frame.loc[0, "oi_valid"] = False
    with pytest.raises(ValueError, match="quarantined OI"):
        crash_data.validate_oi_quantity(frame)
    frame = pd.read_parquet(file)
    frame.loc[1, "oi_valid"] = True
    with pytest.raises(ValueError, match="invalid positive"):
        crash_data.validate_oi_quantity(frame)
    frame = pd.read_parquet(file)
    frame.loc[1, "oi_value"] = 1.0
    with pytest.raises(ValueError, match="quarantined OI"):
        crash_data.validate_oi_quantity(frame)


def test_quarantined_missing_quantity_is_preserved_by_research_loader(tmp_path):
    cfg, _, _, args = anomaly(tmp_path)
    quarantine_day(**args, apply=True)
    root = tmp_path / "data" / "crash"
    restored = crash_data.load(
        root, "oi", "BTCUSDT", before=pd.Timestamp("2024-01-03T00:00:00Z")
    )
    assert restored["oi_valid"].tolist() == [True, False]
    assert np.isnan(restored["oi_qty"].iloc[1])
    candles = pd.DataFrame({
        "ts": pd.date_range("2024-01-01T00:00:00Z", periods=30, freq="min"),
        "close": [100.0] * 30,
    })
    one_minute = bars(candles, restored)
    assert one_minute.loc[pd.Timestamp("2024-01-01T00:15:00Z"), "oi_invalid"]
    assert pd.isna(one_minute.loc[pd.Timestamp("2024-01-01T00:15:00Z"), "oi"])
    assert not one_minute.loc[pd.Timestamp("2024-01-01T00:10:00Z"), "oi_invalid"]


def test_signal_window_blocks_missing_oi_without_future_lookahead():
    # Make all financial filters true after the 1440-minute volume warmup.
    n = 1700
    idx = pd.date_range("2024-01-01", periods=n, freq="min", tz="UTC")
    b = pd.DataFrame({
        "close": 100 * np.power(.999, np.arange(n)),
        "quote_volume": np.full(n, 100.0),
        "oi": 1000 + np.arange(n, dtype=float),
        "oi_invalid": np.zeros(n, dtype=bool),
    }, index=idx)
    b.loc[idx[1600], "oi_invalid"] = True
    settings = CrashSettings.preset(
        "breakdown", drop_pct=.01, window_min=1, vol_mult=0,
        oi_pct=0, vol_target=0,
    )
    active = signals(b, settings)
    assert active[1599]
    assert not active[1600]
    assert not active[1601]
    assert active[1602]


def test_quality_quarantine_rejects_holdout_even_with_correct_hashes(tmp_path):
    _, folder, _, args = anomaly(tmp_path)
    original = (folder / "2024-01-01.parquet").read_bytes()
    with pytest.raises(ValueError, match="protected holdout"):
        quarantine_day(**{**args, "day": "2024-01-03"}, apply=True)
    assert (folder / "2024-01-01.parquet").read_bytes() == original



def test_mixed_valid_and_quarantined_daily_sources_load_without_mask_nulls(tmp_path):
    cfg, folder, _, args = anomaly(tmp_path)
    valid_day = crash_data.parse_metrics(zipped(HEADER + SECOND))
    valid_day.to_parquet(folder / "2024-01-02.parquet", index=False)
    quarantine_day(**args, apply=True)
    loaded = crash_data.load(
        tmp_path / "data" / "crash", "oi", "BTCUSDT",
        before=pd.Timestamp("2024-01-03T00:00:00Z"),
    )
    assert loaded["oi_valid"].tolist() == [True, False, True, True]
    assert loaded["oi_qty"].isna().sum() == 1
    crash_data.validate_oi_quantity(loaded)


def test_reject_malformed_quality_masks_that_would_fake_valid_oi():
    f = pd.DataFrame({
        "ts": pd.date_range("2024-01-01", periods=2, freq="5min", tz="UTC"),
        "oi_qty": [100, np.nan],
        "oi_value": [4200000, 0],
        "oi_valid": [True, False],
    })
    crash_data.validate_oi_quantity(f)
    f.loc[1, "oi_qty"] = 0.0
    with pytest.raises(ValueError, match="quarantined OI"):
        crash_data.validate_oi_quantity(f)
    f.loc[1, "oi_qty"] = np.nan
    f["oi_valid"] = [True, None]
    with pytest.raises(ValueError, match="quality-mask"):
        crash_data.validate_oi_quantity(f)



def test_audit_rejects_quarantine_manifest_missing_timestamps(tmp_path):
    import json

    cfg, _, _, args = anomaly(tmp_path)
    quarantine_day(**args, apply=True)
    manifest = (
        tmp_path / "data" / "crash" / "oi-original-backups"
        / "BTCUSDT" / "2024-01-01.parquet.migration.json"
    )
    meta = json.loads(manifest.read_text())
    meta["quarantine_timestamps"] = []
    manifest.write_text(json.dumps(meta))
    report = audit_migrations(cfg)
    assert not report["ok"]
    assert any("quarantine timestamps differ" in issue for issue in report["issues"])
