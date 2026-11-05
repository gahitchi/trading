"""Source-forensics tests: explicit UTC holdout, no local mutation, no guessing."""

import hashlib

import pandas as pd
import pytest

from lab.crash_oi_forensics import inspect_source
from lab.crash_oi_migration import migrate_batch
from tests.test_crash_oi_migration import (
    DATA,
    HEADER,
    SECOND,
    exchange,
    setup_cache,
    zipped,
)


def test_next_unmigrated_day_source_matches_and_writes_nothing(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=True)
    original = (folder / "2024-01-01.parquet").read_bytes()
    result = inspect_source(cfg, get=exchange)
    assert result["day"] == "2024-01-01"
    assert result["safe_to_migrate_under_current_policy"] is True
    assert result["timestamp_set_match"] is True
    assert result["notional_matches_legacy"] is True
    assert result["nonpositive_or_nonfinite_quantity"] == 0
    assert result["nonpositive_or_nonfinite_notional"] == 0
    assert result["archive_rows"] == 2
    assert result["archive_sha256"] == hashlib.sha256(zipped(HEADER + DATA)).hexdigest()
    assert (folder / "2024-01-01.parquet").read_bytes() == original
    assert not (folder / ".oi-quantity-migration.lock").exists()


def test_nonpositive_quantity_is_reported_without_accepting_source(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    initial = (folder / "2024-01-01.parquet").read_bytes()
    bad = DATA.replace(",105,3990000", ",0,3990000")
    report = inspect_source(cfg, get=lambda _: zipped(HEADER + bad))
    assert not report["safe_to_migrate_under_current_policy"]
    assert report["timestamp_set_match"]
    assert report["notional_matches_legacy"]
    assert report["nonpositive_or_nonfinite_quantity"] == 1
    assert report["nonpositive_or_nonfinite_notional"] == 0
    assert report["first_invalid_rows"][0]["quantity"] == "0"
    assert (folder / "2024-01-01.parquet").read_bytes() == initial
    assert not (folder / ".oi-quantity-migration.lock").exists()


def test_zero_notional_is_reported_as_source_difference(tmp_path):
    (
        cfg,
        _,
    ) = setup_cache(tmp_path, with_second=False, with_holdout=False)
    bad = DATA.replace("3990000", "0")
    report = inspect_source(cfg, get=lambda _: zipped(HEADER + bad))
    assert not report["safe_to_migrate_under_current_policy"]
    assert report["nonpositive_or_nonfinite_notional"] == 1
    assert not report["notional_matches_legacy"]


def test_holdout_day_never_downloaded_or_opened(tmp_path):
    cfg, _ = setup_cache(tmp_path)

    def forbidden(_):
        raise AssertionError("must not fetch protected holdout data")

    with pytest.raises(ValueError, match="protected holdout"):
        inspect_source(cfg, day="2024-01-03", get=forbidden)


def test_partial_batch_error_identifies_failing_day_and_preserves_first_commit(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=True, with_holdout=True)
    initial_second = (folder / "2024-01-02.parquet").read_bytes()

    def fail_on_second(url):
        if "2024-01-02" in url:
            bad = SECOND.replace(",110,4400000", ",0,4400000")
            return zipped(HEADER + bad)
        return exchange(url)

    with pytest.raises(ValueError, match=r"stopped at 2024-01-02.parquet; this invocation already migrated 1"):
        migrate_batch(cfg, limit=2, apply=True, get=fail_on_second)
    assert pd.read_parquet(folder / "2024-01-01.parquet")["oi_qty"].tolist() == [100, 105]
    assert (folder / "2024-01-02.parquet").read_bytes() == initial_second
    # An orphan backup records the failed pre-write attempt and must not be
    # silently ignored when an independent integrity audit is run.
    backup = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    assert (backup / "2024-01-02.parquet").read_bytes() == initial_second
    assert not (backup / "2024-01-02.parquet.migration.json").exists()

    report = inspect_source(cfg, get=fail_on_second)
    assert report["day"] == "2024-01-02"
    assert report["nonpositive_or_nonfinite_quantity"] == 1
