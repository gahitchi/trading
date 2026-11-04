"""Offline verification of guarded, backup-first, pre-holdout OI migration."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from lab import crash_data
from lab.crash_oi_migration import _migrate_one, migrate_batch
from lab.persistence import read_json


HEADER = "create_time,symbol,sum_open_interest,sum_open_interest_value\n"
DATA = (
    "2024-01-01 00:05:00,BTCUSDT,100,4200000\n"
    "2024-01-01 00:10:00,BTCUSDT,105,3990000\n"
)
SECOND = (
    "2024-01-02 00:05:00,BTCUSDT,107,4300000\n"
    "2024-01-02 00:10:00,BTCUSDT,110,4400000\n"
)
HOLDOUT = (
    "2024-01-03 00:05:00,BTCUSDT,120,4700000\n"
)


def zipped(text: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as file:
        file.writestr("data.csv", text)
    return out.getvalue()


def setup_cache(tmp_path, *, with_second=True, with_holdout=True):
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[paths]\ndata = "data"\nresults = "results"\n')
    results = tmp_path / "results"
    results.mkdir()
    (results / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": "2024-01-03T00:00:00+00:00"})
    )
    folder = tmp_path / "data" / "crash" / "oi" / "BTCUSDT"
    folder.mkdir(parents=True)
    for day, data in [
        ("2024-01-01", DATA),
        *(([("2024-01-02", SECOND)] if with_second else [])),
        *(([("2024-01-03", HOLDOUT)] if with_holdout else [])),
    ]:
        file = folder / (day + ".parquet")
        crash_data.parse_metrics(zipped(HEADER + data)).drop(columns="oi_qty").to_parquet(file, index=False)
    return cfg, folder


def exchange(url: str) -> bytes:
    if "2024-01-01" in url:
        return zipped(HEADER + DATA)
    if "2024-01-02" in url:
        return zipped(HEADER + SECOND)
    raise AssertionError("holdout source must never be fetched")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_default_dry_run_is_strictly_read_only(tmp_path):
    cfg, folder = setup_cache(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    output = migrate_batch(cfg, limit=1)
    assert output["dry_run"] is True
    assert output["legacy_remaining"] == 2
    assert output["selected"] == ["2024-01-01.parquet"]
    assert before == {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    assert not (folder / ".oi-quantity-migration.lock").exists()
    assert not (tmp_path / "data" / "crash" / "oi-original-backups").exists()


def test_backup_first_and_automatic_resumable_batches(tmp_path):
    cfg, folder = setup_cache(tmp_path)
    originals = {p.name: p.read_bytes() for p in folder.glob("*.parquet")}
    out = migrate_batch(cfg, limit=1, apply=True, get=exchange)
    assert out["legacy_remaining"] == 1
    assert out["processed"] == [{"file": "2024-01-01.parquet", "result": "migrated"}]
    backup_dir = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    first = folder / "2024-01-01.parquet"
    backup = backup_dir / first.name
    assert backup.read_bytes() == originals[first.name]
    assert pd.read_parquet(first)["oi_qty"].tolist() == [100, 105]
    manifest = read_json(backup_dir / (first.name + ".migration.json"))
    assert manifest["status"] == "committed"
    assert manifest["original_sha256"] == digest(originals[first.name])
    assert manifest["updated_sha256"] == digest(first.read_bytes())
    assert manifest["binance_archive_sha256"] == digest(zipped(HEADER + DATA))
    assert (folder / "2024-01-03.parquet").read_bytes() == originals["2024-01-03.parquet"]
    second = migrate_batch(cfg, limit=1, apply=True, get=exchange)
    assert second["legacy_remaining"] == 0
    assert pd.read_parquet(folder / "2024-01-02.parquet")["oi_qty"].tolist() == [107, 110]
    assert migrate_batch(cfg, limit=2, apply=True, get=exchange)["processed"] == []
    assert (folder / "2024-01-03.parquet").read_bytes() == originals["2024-01-03.parquet"]


def test_archive_notional_mismatch_keeps_source_and_original_backup(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    source = folder / "2024-01-01.parquet"
    original = source.read_bytes()
    wrong = DATA.replace("3990000", "2000000")
    with pytest.raises(ValueError, match="archive notional differs"):
        migrate_batch(cfg, apply=True, limit=1, get=lambda _url: zipped(HEADER + wrong))
    assert source.read_bytes() == original
    backup_dir = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    assert (backup_dir / source.name).read_bytes() == original
    assert not list(folder.glob("*.tmp"))
    assert not list(backup_dir.glob("*.migration.json"))


def test_holdout_partition_is_required_not_created(tmp_path):
    cfg, folder = setup_cache(tmp_path)
    (tmp_path / "results" / "crash_partition.json").unlink()
    with pytest.raises(ValueError, match="partition missing"):
        migrate_batch(cfg, apply=True, get=exchange)
    assert not (folder / ".oi-quantity-migration.lock").exists()


def test_bounds_reject_invalid_batch_or_symbol(tmp_path):
    cfg, _ = setup_cache(tmp_path)
    with pytest.raises(ValueError, match="between 1 and 20"):
        migrate_batch(cfg, limit=858)
    with pytest.raises(ValueError, match="uppercase"):
        migrate_batch(cfg, symbol="../BTCUSDT")
    with pytest.raises(ValueError, match="reserve"):
        migrate_batch(cfg, min_free_mib=0)


def test_interrupted_transaction_is_reconciled_without_downloading(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    original = (folder / "2024-01-01.parquet").read_bytes()
    done = migrate_batch(cfg, apply=True, limit=1, get=exchange)
    assert done["legacy_remaining"] == 0
    backup_dir = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    manifest_path = backup_dir / "2024-01-01.parquet.migration.json"
    manifest = read_json(manifest_path)
    manifest["status"] = "prepared"
    manifest_path.write_text(json.dumps(manifest))
    # The source rename happened before SIGKILL, but metadata commit did not.
    # _migrate_one must reconcile it without fetching from the exchange again.
    result = _migrate_one(
        folder / "2024-01-01.parquet",
        backup_dir / "2024-01-01.parquet",
        symbol="BTCUSDT", boundary=pd.Timestamp("2024-01-03").date(),
        get=lambda _url: (_ for _ in ()).throw(AssertionError("unnecessary fetch")),
    )
    assert result == "already_migrated"
    assert read_json(manifest_path)["status"] == "committed"
    assert (backup_dir / "2024-01-01.parquet").read_bytes() == original


def test_corrupted_backup_blocks_reprocessing(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    before = (folder / "2024-01-01.parquet").read_bytes()
    backup_dir = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    backup_dir.mkdir(parents=True)
    (backup_dir / "2024-01-01.parquet").write_bytes(b"bad")
    with pytest.raises(ValueError, match="backup differs"):
        migrate_batch(cfg, apply=True, get=exchange)
    assert (folder / "2024-01-01.parquet").read_bytes() == before


def test_cannot_modify_when_holdout_boundary_is_not_utc_midnight(tmp_path):
    cfg, folder = setup_cache(tmp_path)
    (tmp_path / "results" / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": "2024-01-03T08:00:00+00:00"})
    )
    with pytest.raises(ValueError, match="UTC day"):
        migrate_batch(cfg, apply=True, get=exchange)
    assert not (folder / ".oi-quantity-migration.lock").exists()



def test_batch_recovers_manifest_after_rename_without_requery(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    first = migrate_batch(cfg, apply=True, get=exchange)
    assert first["legacy_remaining"] == 0
    backup_dir = tmp_path / "data" / "crash" / "oi-original-backups" / "BTCUSDT"
    manifest = backup_dir / "2024-01-01.parquet.migration.json"
    meta = read_json(manifest)
    meta["status"] = "prepared"
    manifest.write_text(json.dumps(meta))

    def fail_fetch(_):
        raise AssertionError("already upgraded data must not be downloaded")

    # Critical: the upgraded Parquet no longer appears in legacy_oi_paths.
    # The batch entry point must still reconcile the pending transaction.
    resumed = migrate_batch(cfg, apply=True, get=fail_fetch)
    assert resumed["legacy_remaining"] == 0
    assert resumed["processed"] == []
    assert resumed["reconciled_transactions"] == ["2024-01-01.parquet"]
    assert read_json(manifest)["status"] == "committed"
    assert pd.read_parquet(folder / "2024-01-01.parquet")["oi_qty"].tolist() == [100, 105]


def test_batch_rejects_corruption_of_previously_committed_day(tmp_path):
    cfg, folder = setup_cache(tmp_path, with_second=False, with_holdout=False)
    migrate_batch(cfg, apply=True, get=exchange)
    source = folder / "2024-01-01.parquet"
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="committed migration source was modified"):
        migrate_batch(cfg, apply=True, get=exchange)


def test_batch_rejects_missing_backup_even_after_source_upgraded(tmp_path):
    cfg, _ = setup_cache(tmp_path, with_second=False, with_holdout=False)
    migrate_batch(cfg, apply=True, get=exchange)
    backup = (
        tmp_path / "data" / "crash" / "oi-original-backups"
        / "BTCUSDT" / "2024-01-01.parquet"
    )
    backup.unlink()
    with pytest.raises(ValueError, match="original backup digest differs"):
        migrate_batch(cfg, apply=True, get=exchange)
