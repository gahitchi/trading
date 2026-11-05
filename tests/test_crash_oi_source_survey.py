"""Offline read-only scanner tests for recurring historical Binance OI zeros."""

import hashlib
import json

import pandas as pd
import pytest

from lab import crash_data
from lab.crash_oi_quarantine_batch import load_pinned_plan
from lab.crash_oi_source_survey import source_survey, write_new_manifest
from tests.test_crash_oi_migration import DATA, HEADER, SECOND, setup_cache, zipped


def fixtures(tmp_path, *, zero_first=True, zero_second=True):
    cfg, folder = setup_cache(tmp_path)
    originals = (DATA, SECOND)
    blobs = {}
    for day, plain, flagged in zip(
        ("2024-01-01", "2024-01-02"),
        originals,
        (zero_first, zero_second),
        strict=True,
    ):
        archive = plain
        if flagged:
            archive = archive.replace(
                ",105,3990000" if day == "2024-01-01" else ",110,4400000",
                ",0,0",
            )
            file = folder / f"{day}.parquet"
            old = pd.read_parquet(file)
            old.loc[1, "oi_value"] = 0.0
            old.to_parquet(file, index=False)
        blobs[day] = zipped(HEADER + archive)

    calls = []

    def fetch(url):
        calls.append(url)
        for day, blob in blobs.items():
            if day in url:
                return blob
        raise AssertionError("out-of-range or holdout archive fetched")

    return cfg, folder, blobs, fetch, calls


def test_two_anomalous_days_produce_fully_verified_plan_without_writes(tmp_path):
    cfg, folder, blobs, get, calls = fixtures(tmp_path)
    original_files = {f.name: f.read_bytes() for f in folder.glob("*.parquet")}
    result = source_survey(cfg, start="2024-01-01", days=2, get=get)
    assert result["read_only"] and result["ready_for_manual_review"]
    assert result["blocked_days"] == []
    assert result["candidate_days"] == 2
    assert result["clean_days"] == 0
    assert result["invalid_zero_pairs"] == 2
    assert [x["status"] for x in result["observations"]] == [
        "exact_zero_pairs",
        "exact_zero_pairs",
    ]
    plan = result["candidate_manifest"]
    assert [x["day"] for x in plan["entries"]] == [
        "2024-01-01",
        "2024-01-02",
    ]
    assert plan["entries"][0]["archive_sha256"] == hashlib.sha256(blobs["2024-01-01"]).hexdigest()
    assert len(calls) == 2
    assert original_files == {f.name: f.read_bytes() for f in folder.glob("*.parquet")}
    assert not (folder / ".oi-quantity-migration.lock").exists()
    assert not (tmp_path / "data" / "crash" / "oi-original-backups").exists()


def test_explicit_plan_file_is_new_only_and_accepted_by_existing_batch_parser(tmp_path):
    cfg, folder, _, get, _ = fixtures(tmp_path)
    result = source_survey(cfg, start="2024-01-01", days=2, get=get)
    target = tmp_path / "operator-review.json"
    write_new_manifest(target, result)
    plan = load_pinned_plan(target, holdout_start=pd.Timestamp("2024-01-03").date())
    assert len(plan["entries"]) == 2
    assert json.loads(target.read_text()) == result["candidate_manifest"]
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_new_manifest(target, result)
    assert not (folder / ".oi-quantity-migration.lock").exists()
    assert not (tmp_path / "data" / "crash" / "oi-original-backups").exists()


def test_clean_days_are_distinguished_from_candidate_quarantine(tmp_path):
    cfg, _, _, get, _ = fixtures(tmp_path, zero_first=False)
    result = source_survey(cfg, start="2024-01-01", days=2, get=get)
    assert result["clean_days"] == 1
    assert result["candidate_days"] == 1
    assert result["observations"][0]["status"] == "clean"
    assert result["observations"][1]["status"] == "exact_zero_pairs"
    assert not result["ready_for_manual_review"]
    assert result["candidate_manifest"] is None
    with pytest.raises(ValueError, match="cannot write a plan"):
        write_new_manifest(tmp_path / "bad.json", result)


@pytest.mark.parametrize("case", ["asymmetric_zero", "negative_pair", "notional_mismatch"])
def test_unsafe_archive_never_becomes_an_exception_candidate(tmp_path, case):
    cfg, folder, blobs, get, _calls = fixtures(tmp_path, zero_first=True, zero_second=False)
    if case == "asymmetric_zero":
        text = DATA.replace(",105,3990000", ",0,0").replace(",0,0", ",105,0")
    elif case == "negative_pair":
        text = DATA.replace(",105,3990000", ",-1,0")
    else:
        text = DATA.replace(",105,3990000", ",0,1")
    blobs["2024-01-01"] = zipped(HEADER + text)
    result = source_survey(cfg, start="2024-01-01", days=2, get=get)
    assert result["blocked_days"] == ["2024-01-01"]
    assert result["observations"][0]["status"] == "blocked"
    assert result["candidate_days"] == 0
    assert result["candidate_manifest"] is None
    assert not (folder / ".oi-quantity-migration.lock").exists()


def test_holdout_range_is_rejected_before_fetch_or_open(tmp_path):
    cfg, folder, _, _, _ = fixtures(tmp_path)
    original = (folder / "2024-01-03.parquet").read_bytes()
    with pytest.raises(ValueError, match="protected holdout"):
        source_survey(
            cfg,
            start="2024-01-02",
            days=2,
            get=lambda _: (_ for _ in ()).throw(AssertionError("no fetch permitted")),
        )
    assert (folder / "2024-01-03.parquet").read_bytes() == original


@pytest.mark.parametrize("days", [0, -1, 21, True])
def test_bounded_days_reject_invalid_counts(tmp_path, days):
    cfg, _, _, _, _ = fixtures(tmp_path)
    with pytest.raises(ValueError, match="survey days"):
        source_survey(cfg, start="2024-01-01", days=days)


def test_missing_day_blocks_plan_and_does_not_skip_it(tmp_path):
    cfg, folder, _, get, calls = fixtures(tmp_path)
    (folder / "2024-01-02.parquet").unlink()
    result = source_survey(cfg, start="2024-01-01", days=2, get=get)
    assert result["blocked_days"] == ["2024-01-02"]
    assert result["candidate_days"] == 1
    assert result["candidate_manifest"] is None
    assert len(calls) == 1


def test_already_migrated_day_is_blocked_without_redownloading(tmp_path):
    cfg, folder, blobs, get, calls = fixtures(tmp_path, zero_first=False)
    migrated = crash_data.parse_metrics(blobs["2024-01-01"])
    migrated.to_parquet(folder / "2024-01-01.parquet", index=False)
    result = source_survey(cfg, start="2024-01-01", days=1, get=get)
    assert result["blocked_days"] == ["2024-01-01"]
    assert calls == []
    assert result["candidate_manifest"] is None
