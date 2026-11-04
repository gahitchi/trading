"""Isolated pre-holdout five-minute coverage tests, no network or writes by auditor."""

import json
from pathlib import Path

import pandas as pd
import pytest

from lab.crash_oi_coverage import audit_coverage, main


def make_cache(tmp_path, *, days=(1, 2), lengths=None):
    lengths = lengths or {}
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[paths]\ndata = "data"\nresults = "results"\n')
    results = tmp_path / "results"
    results.mkdir()
    (results / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": "2024-01-04T00:00:00+00:00"})
    )
    folder = tmp_path / "data" / "crash" / "oi" / "BTCUSDT"
    backup = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    folder.mkdir(parents=True)
    backup.mkdir(parents=True)
    for n in days:
        day = f"2024-01-{n:02d}"
        stamps = pd.date_range(f"{day}T00:00:00Z", periods=288, freq="5min")
        if n in lengths:
            stamps = stamps[~stamps.isin(lengths[n])]
        pd.DataFrame({"ts": stamps, "oi_qty": [100.0] * len(stamps)}).to_parquet(
            folder / f"{day}.parquet", index=False
        )
        (backup / f"{day}.parquet.migration.json").write_text(
            json.dumps({
                "status": "committed",
                "source_name": f"{day}.parquet",
                "symbol": "BTCUSDT",
            })
        )
    # Deliberately not migrated or listed in a manifest.
    pd.DataFrame({
        "ts": [pd.Timestamp("2024-01-04T00:00:00Z")],
        "oi_value": [100000.0],
    }).to_parquet(folder / "2024-01-04.parquet", index=False)
    return cfg, folder, backup


def test_complete_migrated_days_pass_even_with_unmigrated_holdout(tmp_path, monkeypatch):
    cfg, folder, _ = make_cache(tmp_path)
    original = pd.read_parquet

    def guarded_read(path, *args, **kwargs):
        assert Path(path).name != "2024-01-04.parquet", "read protected holdout"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", guarded_read)
    report = audit_coverage(cfg)
    assert report["coverage_ok_for_checked_days"]
    assert report["checked_days"] == 2
    assert report["observations"] == report["full_day_count_benchmark"] == 576
    assert report["abnormal_days"] == []
    assert report["integrity_verified"] is False
    assert report["read_only"] is True


def test_125_missing_snapshots_are_identified_on_correct_day(tmp_path):
    missing = pd.date_range("2024-01-02T06:00:00Z", periods=125, freq="5min")
    cfg, _, _ = make_cache(tmp_path, lengths={2: missing})
    report = audit_coverage(cfg)
    assert not report["coverage_ok_for_checked_days"]
    assert report["count_shortfall"] == 125
    assert report["observations"] == 451
    assert len(report["abnormal_days"]) == 1
    problem = report["abnormal_days"][0]
    assert problem["day"] == "2024-01-02"
    assert problem["observations"] == 163
    assert problem["count_shortfall"] == 125
    assert problem["internal_missing_slots"] == 125
    assert problem["off_five_minute_grid"] == 0
    assert problem["missing_slots"] == 125
    assert problem["duplicate_timestamps"] == 0


def test_nonmonotonic_but_complete_archive_has_advisory_not_missing_slots(tmp_path):
    cfg, folder, _ = make_cache(tmp_path, days=(1,))
    p = folder / "2024-01-01.parquet"
    df = pd.read_parquet(p)
    df.loc[[50, 51], "ts"] = df.loc[[51, 50], "ts"].to_numpy()
    df.to_parquet(p, index=False)
    report = audit_coverage(cfg)
    assert report["coverage_ok_for_checked_days"]
    assert report["count_shortfall"] == 0
    assert report["abnormal_days"] == []
    assert report["ordering_only_day_count"] == 1
    assert report["ordering_only_days"][0]["raw_order_inversions"] == 1


def test_missing_migrated_calendar_day_is_reported_without_opening_data(tmp_path):
    cfg, _, _ = make_cache(tmp_path, days=(1, 3))
    report = audit_coverage(cfg)
    assert not report["coverage_ok_for_checked_days"]
    assert report["missing_calendar_days_within_checked_range"] == ["2024-01-02"]


def test_holdout_manifest_is_rejected_without_reading_holdout(tmp_path, monkeypatch):
    cfg, _, backups = make_cache(tmp_path, days=(1,))
    (backups / "2024-01-04.parquet.migration.json").write_text("{}")
    original = pd.read_parquet

    def guarded_read(path, *args, **kwargs):
        assert Path(path).name != "2024-01-04.parquet", "read protected holdout"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", guarded_read)
    report = audit_coverage(cfg)
    assert not report["coverage_ok_for_checked_days"]
    assert "protected holdout" in report["errors"][0]


def test_empty_scope_cannot_be_declared_complete(tmp_path):
    cfg, _, backups = make_cache(tmp_path, days=())
    result = audit_coverage(cfg)
    assert not result["coverage_ok_for_checked_days"]
    assert result["checked_days"] == 0
    assert result["abnormal_days"] == []
    assert not list(backups.glob("*.parquet.migration.json"))


def test_cli_returns_nonzero_on_missing_observations(tmp_path, capsys):
    missing = pd.date_range("2024-01-02T06:00:00Z", periods=125, freq="5min")
    cfg, _, _ = make_cache(tmp_path, lengths={2: missing})
    assert main(["--config", str(cfg)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["count_shortfall"] == 125
    assert out["coverage_ok_for_checked_days"] is False



def test_tail_gap_is_not_confused_with_an_internal_gap(tmp_path):
    # Matches the observed 2024-02-16 short day: 00:00 through 13:30 only.
    missing = pd.date_range("2024-01-02T13:35:00Z", periods=125, freq="5min")
    cfg, _, _ = make_cache(tmp_path, lengths={2: missing})
    report = audit_coverage(cfg)
    day = report["abnormal_days"][0]
    assert day["missing_slots"] == 125
    assert day["internal_missing_slots"] == 0
    assert day["trailing_missing_slots"] == 125
    assert day["leading_missing_slots"] == 0
    assert day["missing_ranges"] == [{
        "start": "2024-01-02T13:35:00+00:00",
        "end": "2024-01-02T23:55:00+00:00",
        "slots": 125,
    }]


def test_288_shuffled_complete_rows_have_zero_missing_and_no_failure(tmp_path):
    cfg, folder, _ = make_cache(tmp_path, days=(1,))
    p = folder / "2024-01-01.parquet"
    original = pd.read_parquet(p)
    original.sample(frac=1, random_state=102).reset_index(drop=True).to_parquet(p, index=False)
    report = audit_coverage(cfg)
    assert report["coverage_ok_for_checked_days"]
    assert report["observations"] == report["full_day_count_benchmark"] == 288
    assert report["ordering_only_day_count"] == 1
    assert report["abnormal_days"] == []
    assert report["ordering_only_days"][0]["raw_order_inversions"] > 0


def test_duplicate_replaces_missing_slot_even_when_row_count_is_288(tmp_path):
    cfg, folder, _ = make_cache(tmp_path, days=(1,))
    p = folder / "2024-01-01.parquet"
    df = pd.read_parquet(p)
    df.loc[100, "ts"] = df.loc[99, "ts"]
    df.to_parquet(p, index=False)
    report = audit_coverage(cfg)
    assert not report["coverage_ok_for_checked_days"]
    assert report["count_shortfall"] == 0
    day = report["abnormal_days"][0]
    assert day["duplicate_timestamps"] == 1
    assert day["unique_observations"] == 287
    assert day["missing_slots"] == 1
    assert day["internal_missing_slots"] == 1
    assert day["raw_order_inversions"] == 0


def test_offgrid_snapshot_is_never_silently_accepted(tmp_path):
    cfg, folder, _ = make_cache(tmp_path, days=(1,))
    p = folder / "2024-01-01.parquet"
    df = pd.read_parquet(p)
    df.loc[77, "ts"] = df.loc[77, "ts"] + pd.Timedelta(minutes=1)
    df.to_parquet(p, index=False)
    report = audit_coverage(cfg)
    assert not report["coverage_ok_for_checked_days"]
    day = report["abnormal_days"][0]
    assert day["off_five_minute_grid"] == 1
    assert day["missing_slots"] == 1
    assert day["unexpected_slots"] == 1


def test_period_end_convention_ending_next_midnight_is_accepted(tmp_path):
    cfg, folder, _ = make_cache(tmp_path, days=(1,))
    p = folder / "2024-01-01.parquet"
    frame = pd.read_parquet(p)
    frame["ts"] = frame["ts"] + pd.Timedelta(minutes=5)
    frame.to_parquet(p, index=False)
    report = audit_coverage(cfg)
    assert report["coverage_ok_for_checked_days"]
    assert report["abnormal_days"] == []
    assert report["ordering_only_day_count"] == 0
    assert report["observations"] == 288
