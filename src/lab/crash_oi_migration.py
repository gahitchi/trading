"""Bounded, auditable migration of historical Binance OI quantity caches.

Run against the ORIGINAL research checkout's lab.toml; the cutoff is read
from its existing crash_partition.json, never guessed or initialized.
Default behavior is read-only. --apply backs up raw Parquet bytes before
mutating any pre-holdout day, with fsynced per-file transaction metadata.

This is cache maintenance only; it does not run a trading strategy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from lab import crash_data
from lab.config import load_config
from lab.market_symbols import SYMBOL_RE
from lab.persistence import atomic_json, file_lock, read_json

MIN_FREE_MIB = 128
MAX_BATCH = 20


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _holdout_boundary(cfg) -> date:
    path = cfg.results_dir / "crash_partition.json"
    if not path.is_file():
        raise ValueError(f"protected crash holdout partition missing: {path}")
    part = read_json(path)
    if not isinstance(part, dict) or part.get("version") != 1 or not part.get("holdout_start"):
        raise ValueError(f"invalid crash holdout partition: {path}")
    boundary = pd.Timestamp(part["holdout_start"])
    if boundary.tzinfo is None or boundary.utcoffset() != pd.Timedelta(0):
        raise ValueError("crash holdout boundary must be UTC")
    if boundary != boundary.floor("D"):
        raise ValueError("crash holdout boundary must start on a UTC day")
    return boundary.date()


def _headroom(directory: Path, minimum_mib: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < minimum_mib * 1024 * 1024:
        raise RuntimeError(
            f"insufficient disk space: {free / 1048576:.0f} MiB free, "
            f"need at least {minimum_mib} MiB"
        )


def _backup(source: Path, backup: Path) -> str:
    """Create immutable exact-byte backup; never overwrite an existing one."""
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"unsafe or missing source file: {source}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    source_sha = _sha(source)
    if backup.exists():
        if backup.is_symlink() or not backup.is_file() or _sha(backup) != source_sha:
            raise ValueError(f"existing backup differs from source; manual recovery required: {backup}")
        return source_sha

    tmp = backup.with_name(backup.name + ".partial")
    if tmp.exists():
        # Prior interrupted byte copy: never trust it; regenerate from intact source.
        if tmp.is_symlink() or not tmp.is_file():
            raise ValueError(f"unexpected backup temporary file: {tmp}")
        tmp.unlink()
    try:
        with source.open("rb") as inp, tmp.open("xb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        if _sha(tmp) != source_sha:
            raise ValueError(f"backup byte verification failed: {backup}")
        if backup.exists():
            raise FileExistsError(f"backup appeared during copy: {backup}")
        os.replace(tmp, backup)
        _sync_directory(backup.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return source_sha


def _quantity_from_original(source: Path, archive_blob: bytes) -> tuple[pd.DataFrame, int]:
    """Match every original timestamp and notional value before adding quantity."""
    old = pd.read_parquet(source)
    if "oi_qty" in old and old["oi_qty"].notna().all():
        raise ValueError(f"file is already migrated: {source}")
    if not {"ts", "oi_value"} <= set(old):
        raise ValueError(f"invalid legacy schema: {source}")
    old_ts = pd.to_datetime(old["ts"], utc=True, errors="raise")
    if old_ts.isna().any() or old_ts.duplicated().any():
        raise ValueError(f"invalid legacy timestamps: {source}")
    new = crash_data.parse_metrics(archive_blob)
    new_ts = pd.to_datetime(new["ts"], utc=True)
    if len(old) != len(new) or not np.array_equal(
        np.sort(old_ts.to_numpy()), np.sort(new_ts.to_numpy())
    ):
        raise ValueError(f"original Binance archive timestamps differ: {source}")
    old_values = pd.to_numeric(old["oi_value"], errors="raise").to_numpy(dtype=float)
    if not np.isfinite(old_values).all() or (old_values <= 0).any():
        raise ValueError(f"invalid legacy OI notional: {source}")
    aligned = new.set_index("ts").reindex(pd.DatetimeIndex(old_ts))
    if not np.array_equal(old_values, aligned["oi_value"].to_numpy(dtype=float)):
        raise ValueError(f"original Binance archive notional differs: {source}")

    upgraded = old.copy()
    upgraded["oi_qty"] = aligned["oi_qty"].to_numpy(dtype=float)
    if not np.isfinite(upgraded["oi_qty"].to_numpy()).all():
        raise ValueError(f"invalid quantity after aligning source: {source}")
    return upgraded, len(old)


def _reconcile_manifest(source: Path, backup: Path, manifest: Path) -> None:
    """Finish a crash between data rename and committing the transaction."""
    if not manifest.exists():
        return
    meta = read_json(manifest)
    if meta.get("schema_version") != 1 or meta.get("source_name") != source.name:
        raise ValueError(f"unknown transaction manifest: {manifest}")
    if not backup.exists() or _sha(backup) != meta.get("original_sha256"):
        raise ValueError(f"original backup digest differs: {backup}")
    if not source.exists():
        raise ValueError(f"migrated source missing: {source}")
    digest = _sha(source)
    if meta.get("status") == "committed":
        if digest != meta.get("updated_sha256"):
            raise ValueError(f"committed migration source was modified: {source}")
    elif meta.get("status") == "prepared":
        if digest == meta.get("updated_sha256"):
            meta["status"] = "committed"
            atomic_json(manifest, meta)
        elif digest != meta.get("original_sha256"):
            raise ValueError(f"interrupted migration has unexpected source bytes: {source}")
    else:
        raise ValueError(f"unknown migration state: {manifest}")


def _migrate_one(
    source: Path,
    backup: Path,
    *,
    symbol: str,
    boundary: date,
    get,
) -> str:
    """Full transaction for one known-legacy, pre-boundary daily cache."""
    if source.stem.count("-") != 2 or date.fromisoformat(source.stem) >= boundary:
        raise ValueError("refusing to modify holdout or non-daily open-interest file")
    manifest = backup.with_name(source.name + ".migration.json")
    _reconcile_manifest(source, backup, manifest)
    current = pd.read_parquet(source)
    if "oi_qty" in current and current["oi_qty"].notna().all():
        return "already_migrated"

    original_sha = _backup(source, backup)
    blob = get(crash_data.url_of("oi", symbol, source.stem))
    updated, records = _quantity_from_original(source, blob)

    tmp = source.with_name(source.name + ".oi-quantity.tmp")
    if tmp.exists() and (tmp.is_symlink() or not tmp.is_file()):
        raise ValueError(f"unsafe temporary file: {tmp}")
    tmp.unlink(missing_ok=True)
    try:
        updated.to_parquet(tmp, index=False)
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        reread = pd.read_parquet(tmp)
        if not reread.equals(updated):
            raise ValueError(f"Parquet write verification failed: {source}")
        updated_sha = _sha(tmp)
        if _sha(source) != original_sha:
            raise ValueError(f"source changed during migration: {source}")
        info = {
            "schema_version": 1,
            "source_name": source.name,
            "symbol": symbol,
            "holdout_start": boundary.isoformat(),
            "records": records,
            "original_sha256": original_sha,
            "updated_sha256": updated_sha,
            "binance_archive_sha256": hashlib.sha256(blob).hexdigest(),
            "status": "prepared",
        }
        atomic_json(manifest, info)
        os.replace(tmp, source)
        _sync_directory(source.parent)
        if _sha(source) != updated_sha:
            raise ValueError(f"migration verification after replace failed: {source}")
        info["status"] = "committed"
        atomic_json(manifest, info)
        return "migrated"
    finally:
        tmp.unlink(missing_ok=True)



def _reconcile_prior_transactions(
    folder: Path,
    backup_dir: Path,
    *,
    symbol: str,
    boundary: date,
) -> list[str]:
    """Validate all previous transactions and finish interrupted commits.

    After an atomic Parquet replacement, the upgraded file is no longer
    considered legacy; therefore reconciling *only* legacy files misses the
    precise SIGKILL window between rename and manifest commit. Reconcile
    all existing manifests before selecting the next migration batch.
    """
    if not backup_dir.exists():
        return []
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise ValueError(f"unsafe migration backup directory: {backup_dir}")
    reconciled = []
    for manifest in sorted(backup_dir.glob("*.parquet.migration.json")):
        if manifest.is_symlink():
            raise ValueError(f"unsafe transaction manifest: {manifest}")
        filename = manifest.name.removesuffix(".migration.json")
        if filename != Path(filename).name or not filename.endswith(".parquet"):
            raise ValueError(f"invalid transaction filename: {manifest}")
        try:
            day = date.fromisoformat(Path(filename).stem)
        except ValueError as exc:
            raise ValueError(f"invalid transaction date: {manifest}") from exc
        if day >= boundary:
            raise ValueError(f"transaction touches protected holdout: {manifest}")
        meta = read_json(manifest)
        if meta.get("symbol") != symbol:
            raise ValueError(f"transaction symbol differs: {manifest}")
        try:
            recorded_boundary = date.fromisoformat(meta["holdout_start"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid transaction holdout boundary: {manifest}") from exc
        if recorded_boundary > boundary or day >= recorded_boundary:
            raise ValueError(f"transaction exceeds its recorded holdout boundary: {manifest}")
        source = folder / filename
        backup = backup_dir / filename
        prior_status = meta.get("status")
        _reconcile_manifest(source, backup, manifest)
        if prior_status == "prepared" and read_json(manifest).get("status") == "committed":
            reconciled.append(filename)
    return reconciled

def migrate_batch(
    config_path: Path,
    symbol: str = "BTCUSDT",
    *,
    limit: int = 2,
    apply: bool = False,
    get=crash_data.get_bytes,
    min_free_mib: int = MIN_FREE_MIB,
) -> dict:
    """Dry-run by default; apply at most MAX_BATCH verified pre-holdout days."""
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be an uppercase perpetual symbol")
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH}")
    if min_free_mib < MIN_FREE_MIB:
        raise ValueError(f"minimum free-space reserve must be at least {MIN_FREE_MIB} MiB")
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    root = cfg.data_dir / "crash"
    folder = root / "oi" / symbol
    if not folder.is_dir():
        raise ValueError(f"OI cache directory missing: {folder}")
    backup_dir = root / "oi-original-backups" / symbol
    lock_path = folder / ".oi-quantity-migration.lock"
    # Dry-run does not create a lock file or any other artifacts.
    if not apply:
        old = crash_data.legacy_oi_paths(root, symbol, boundary)
        return {
            "dry_run": True,
            "boundary": boundary.isoformat(),
            "legacy_remaining": len(old),
            "selected": [p.name for p in old[:limit]],
            "backup_dir": str(backup_dir),
        }

    # Live migration serialized with the old migrate_oi_quantity implementation.
    with file_lock(lock_path):
        _headroom(folder, min_free_mib)
        reconciled = _reconcile_prior_transactions(
            folder, backup_dir, symbol=symbol, boundary=boundary
        )
        old = crash_data.legacy_oi_paths(root, symbol, boundary)
        fetch = crash_data.patient(get)
        migrated = []
        for source in old[:limit]:
            try:
                _headroom(folder, min_free_mib)
                result = _migrate_one(
                    source, backup_dir / source.name,
                    symbol=symbol, boundary=boundary, get=fetch,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                # An earlier day in this batch may already be committed.
                # Never present the whole batch as atomic or retry it blindly.
                completed = [item["file"] for item in migrated]
                raise ValueError(
                    f"OI migration stopped at {source.name}; "
                    f"this invocation already migrated {len(completed)} "
                    f"preceding files ({', '.join(completed) or 'none'}); "
                    f"cause: {exc}. Audit committed days before retrying."
                ) from exc
            migrated.append({"file": source.name, "result": result})
        left = crash_data.legacy_oi_paths(root, symbol, boundary)
        return {
            "dry_run": False,
            "boundary": boundary.isoformat(),
            "legacy_remaining": len(left),
            "processed": migrated,
            "reconciled_transactions": reconciled,
            "backup_dir": str(backup_dir),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely migrate pre-holdout Binance open-interest Parquet in small batches"
    )
    parser.add_argument("--config", type=Path, required=True, help="original research checkout's lab.toml")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--limit", type=int, default=2, help=f"1-{MAX_BATCH} days per invocation; default 2")
    parser.add_argument("--apply", action="store_true", help="write verified backups and migrate selected days")
    args = parser.parse_args(argv)
    try:
        result = migrate_batch(args.config, args.symbol, limit=args.limit, apply=args.apply)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
