"""The migration audit is a read-only, fail-closed checksum and schema check."""

import json

import pandas as pd
import pytest

from lab.crash_oi_audit import audit_migrations, main
from lab.crash_oi_migration import migrate_batch
from tests.test_crash_oi_migration import exchange, setup_cache


def test_empty_audit_is_read_only_and_reports_legacy_data(tmp_path):
    cfg, folder = setup_cache(tmp_path)
    result = audit_migrations(cfg)
    assert result["ok"]
    assert result["read_only"]
    assert result["verified_files"] == 0
    assert result["legacy_remaining"] == 2
    assert result["verified_records"] == 0
    assert not (folder / ".oi-quantity-migration.lock").exists()


def test_audits_all_committed_migrations_and_preserves_original_columns(tmp_path):
    cfg, folder = setup_cache(tmp_path)
    migrate_batch(cfg, apply=True, limit=2, get=exchange)
    before = {path.name: path.read_bytes() for path in folder.glob("*.parquet")}
    result = audit_migrations(cfg)
    assert result["ok"], result["issues"]
    assert result["verified_files"] == 2
    assert result["verified_records"] == 4
    assert result["legacy_remaining"] == 0
    assert result["first_verified"] == "2024-01-01.parquet"
    assert result["last_verified"] == "2024-01-02.parquet"
    assert before == {path.name: path.read_bytes() for path in folder.glob("*.parquet")}
    assert pd.read_parquet(folder / "2024-01-03.parquet").columns.tolist() == ["ts", "oi_value"]


@pytest.mark.parametrize("which", ["source", "backup"])
def test_audit_catches_corrupted_content_hashes(tmp_path, which):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    migrate_batch(cfg, apply=True, get=exchange)
    if which == "source":
        target = folder / "2024-01-01.parquet"
        expected = "updated Parquet SHA-256 mismatch"
    else:
        target = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT" / "2024-01-01.parquet"
        expected = "original backup SHA-256 mismatch"
    target.write_bytes(b"tampered")
    result = audit_migrations(cfg)
    assert not result["ok"]
    assert any(expected in issue for issue in result["issues"])


def test_orphan_backup_is_reported_as_unfinished(tmp_path):
    cfg, _ = setup_cache(tmp_path, with_second=False, with_holdout=False)
    backup_dir = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    backup_dir.mkdir(parents=True)
    (backup_dir / "2024-01-01.parquet").write_bytes(b"valid original backup not yet accompanied by manifest")
    result = audit_migrations(cfg)
    assert not result["ok"]
    assert any("backup lacks transaction manifest" in issue for issue in result["issues"])


def test_prepared_manifest_causes_nonzero_exit_and_remains_unchanged(tmp_path, capsys):
    cfg, _ = setup_cache(tmp_path, with_second=False, with_holdout=False)
    migrate_batch(cfg, apply=True, get=exchange)
    manifest = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT" / "2024-01-01.parquet.migration.json"
    item = json.loads(manifest.read_text())
    item["status"] = "prepared"
    manifest.write_text(json.dumps(item))
    assert main(["--config", str(cfg)]) == 1
    result = capsys.readouterr().out
    assert "not fully committed" in result
    assert json.loads(manifest.read_text())["status"] == "prepared"


def test_manifest_targeting_holdout_does_not_open_holdout_file(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=True)
    backup_dir = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    backup_dir.mkdir(parents=True)
    (backup_dir / "2024-01-03.parquet.migration.json").write_text("{}")
    holdout = folder / "2024-01-03.parquet"
    before = holdout.read_bytes()
    result = audit_migrations(cfg)
    assert not result["ok"]
    assert any("protected holdout" in issue for issue in result["issues"])
    assert holdout.read_bytes() == before
