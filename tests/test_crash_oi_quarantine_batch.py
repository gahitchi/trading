"""No network. Strict dry-run preflight before a bounded series of atomic days."""

import hashlib
import json

import pandas as pd
import pytest

from lab.crash_oi_audit import audit_migrations
from lab.crash_oi_quarantine_batch import (
    execute_pinned_plan, load_pinned_plan, main,
)
from tests.test_crash_oi_migration import (
    DATA, HEADER, SECOND, setup_cache, zipped,
)


def sample_plan(tmp_path):
    cfg, folder = setup_cache(tmp_path)
    archives = {
        "2024-01-01": zipped(HEADER + DATA.replace(",105,3990000", ",0,0")),
        "2024-01-02": zipped(HEADER + SECOND.replace(",110,4400000", ",0,0")),
    }
    entries = []
    for day, blob in archives.items():
        p = folder / f"{day}.parquet"
        old = pd.read_parquet(p)
        old.loc[1, "oi_value"] = 0.0
        old.to_parquet(p, index=False)
        entries.append({
            "day": day,
            "archive_sha256": hashlib.sha256(blob).hexdigest(),
            "legacy_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "expect_zero_pairs": 1,
        })
    plan = {
        "schema_version": 1,
        "symbol": "BTCUSDT",
        "holdout_start": "2024-01-03",
        "entries": entries,
    }
    plan_path = tmp_path / "pinned.json"
    plan_path.write_text(json.dumps(plan))
    def fetch(url):
        for day, blob in archives.items():
            if day in url:
                return blob
        raise AssertionError("protected holdout must never be fetched")
    return cfg, folder, plan_path, plan, fetch


def test_two_day_preflight_is_read_only_and_validates_every_hash(tmp_path):
    cfg, folder, path, _, get = sample_plan(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    report = execute_pinned_plan(cfg, path, get=get)
    assert report["read_only"]
    assert report["preflight_ok"]
    assert report["checked_days"] == 2
    assert report["total_zero_pairs"] == 2
    assert [x["day"] for x in report["days"]] == ["2024-01-01", "2024-01-02"]
    assert before == {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    assert not (folder / ".oi-quantity-migration.lock").exists()
    assert not (tmp_path / "data" / "crash" / "oi-original-backups").exists()


def test_mismatch_in_later_day_rejects_apply_before_writing_first_day(tmp_path):
    cfg, folder, path, plan, get = sample_plan(tmp_path)
    plan["entries"][1]["archive_sha256"] = "f" * 64
    path.write_text(json.dumps(plan))
    before = {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    with pytest.raises(ValueError, match="read-only preflight failed at 2024-01-02; zero days modified"):
        execute_pinned_plan(cfg, path, apply=True, get=get)
    assert before == {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    assert not (tmp_path / "data" / "crash" / "oi-original-backups").exists()


def test_apply_audits_each_commit_and_preserves_holdout(tmp_path):
    cfg, folder, path, _, get = sample_plan(tmp_path)
    protected = (folder / "2024-01-03.parquet").read_bytes()
    result = execute_pinned_plan(cfg, path, apply=True, get=get)
    assert result["migration_integrity_ok"]
    assert result["committed_days"] == ["2024-01-01", "2024-01-02"]
    assert result["quarantined_zero_pairs"] == 2
    assert result["verified_files"] == 2
    assert result["verified_records"] == 4
    assert result["legacy_remaining"] == 0
    assert (folder / "2024-01-03.parquet").read_bytes() == protected
    for day in ("2024-01-01", "2024-01-02"):
        f = pd.read_parquet(folder / f"{day}.parquet")
        assert f["oi_valid"].tolist() == [True, False]
        assert pd.isna(f["oi_qty"].iloc[1])
    assert audit_migrations(cfg)["ok"]


@pytest.mark.parametrize("invalid", [
    "duplicate",
    "out_of_order",
    "wrong_holdout",
    "wrong_schema",
    "missing_sha",
    "too_many",
])
def test_malformed_pin_manifest_fails_closed_before_archive_fetch(tmp_path, invalid):
    cfg, folder, path, plan, get = sample_plan(tmp_path)
    if invalid == "duplicate":
        plan["entries"][1] = dict(plan["entries"][0])
    elif invalid == "out_of_order":
        plan["entries"].reverse()
    elif invalid == "wrong_holdout":
        plan["holdout_start"] = "2024-02-01"
    elif invalid == "wrong_schema":
        plan["schema_version"] = 2
    elif invalid == "missing_sha":
        del plan["entries"][0]["archive_sha256"]
    elif invalid == "too_many":
        plan["entries"] = plan["entries"] * 4
    path.write_text(json.dumps(plan))
    before = {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    with pytest.raises(ValueError):
        execute_pinned_plan(
            cfg, path, apply=True,
            get=lambda url: (_ for _ in ()).throw(AssertionError("no downloads before plan validated")),
        )
    assert before == {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    assert not (tmp_path / "data" / "crash" / "oi-original-backups").exists()


def test_protected_holdout_day_is_rejected_before_any_read(tmp_path):
    cfg, folder, path, plan, _ = sample_plan(tmp_path)
    plan["entries"] = [{**plan["entries"][0], "day": "2024-01-03"}]
    path.write_text(json.dumps(plan))
    holdout_before = (folder / "2024-01-03.parquet").read_bytes()
    with pytest.raises(ValueError, match="protected holdout"):
        execute_pinned_plan(cfg, path, apply=True, get=lambda _: None)
    assert (folder / "2024-01-03.parquet").read_bytes() == holdout_before


def test_cli_invalid_plan_gives_nonzero_without_writes(tmp_path, capsys):
    cfg, folder, path, plan, _ = sample_plan(tmp_path)
    plan["holdout_start"] = "2024-04-01"
    path.write_text(json.dumps(plan))
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(cfg), "--manifest", str(path), "--apply"])
    assert exc.value.code == 2
    assert "holdout mismatch" in capsys.readouterr().err
    assert not (folder / ".oi-quantity-migration.lock").exists()


def test_post_commit_audit_failure_stops_before_second_day(tmp_path):
    cfg, folder, path, _, get = sample_plan(tmp_path)
    # A future-day orphan backup is not ignored by the audit. The
    # first day commits, then audit correctly aborts before the second.
    backup = (
        tmp_path / "data" / "crash" / "oi-original-backups"
        / "BTCUSDT"
    )
    backup.mkdir(parents=True)
    (backup / "2024-01-02.parquet").write_bytes(
        (folder / "2024-01-02.parquet").read_bytes()
    )
    with pytest.raises(ValueError, match="batch stopped at 2024-01-01 after 1 committed"):
        execute_pinned_plan(cfg, path, apply=True, get=get)
    assert pd.read_parquet(folder / "2024-01-01.parquet")["oi_valid"].tolist() == [True, False]
    assert "oi_qty" not in pd.read_parquet(folder / "2024-01-02.parquet")
