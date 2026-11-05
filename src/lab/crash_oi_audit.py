"""Read-only verification of historical Binance OI cache migration transactions.

Does not create locks, initialize a holdout partition, download market data,
or modify cached files. Reads only transaction artifacts and pre-holdout OI
Parquet files; protected holdout files are not opened.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from lab import crash_data
from lab.config import load_config
from lab.crash_oi_migration import _holdout_boundary, _sha
from lab.market_symbols import SYMBOL_RE
from lab.persistence import read_json


def audit_migrations(config_path: Path, symbol: str = "BTCUSDT") -> dict:
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be an uppercase perpetual symbol")
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    root = cfg.data_dir / "crash"
    sources = root / "oi" / symbol
    backups = root / "oi-original-backups" / symbol
    if not sources.is_dir() or sources.is_symlink():
        raise ValueError(f"unsafe or missing OI source directory: {sources}")
    if backups.exists() and (backups.is_symlink() or not backups.is_dir()):
        raise ValueError(f"unsafe OI backup directory: {backups}")

    issues: list[str] = []
    verified = 0
    records = 0
    names: set[str] = set()
    verified_names: list[str] = []
    # Never scan into the protected holdout Parquet data.
    manifests = sorted(backups.glob("*.parquet.migration.json"))
    for manifest in manifests:
        name = manifest.name.removesuffix(".migration.json")
        try:
            if name in names or name != Path(name).name or not name.endswith(".parquet"):
                raise ValueError("invalid or repeated transaction filename")
            names.add(name)
            if date.fromisoformat(Path(name).stem) >= boundary:
                raise ValueError("manifest targets protected holdout period")
            if manifest.is_symlink():
                raise ValueError("transaction manifest is a symlink")

            meta = read_json(manifest)
            if not isinstance(meta, dict) or (
                meta.get("schema_version") != 1
                or meta.get("source_name") != name
                or meta.get("symbol") != symbol
                or meta.get("status") != "committed"
            ):
                raise ValueError("transaction is missing or not fully committed")
            recorded_boundary = date.fromisoformat(meta["holdout_start"])
            if recorded_boundary > boundary or date.fromisoformat(Path(name).stem) >= recorded_boundary:
                raise ValueError("manifest holdout boundary is inconsistent")
            if type(meta.get("records")) is not int or meta["records"] < 1:
                raise ValueError("invalid manifest record count")
            if not isinstance(meta.get("binance_archive_sha256"), str) or len(meta["binance_archive_sha256"]) != 64:
                raise ValueError("missing Binance archive digest")

            original = backups / name
            upgraded = sources / name
            for path in (original, upgraded):
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"missing or unsafe Parquet file: {path.name}")
            if _sha(original) != meta.get("original_sha256"):
                raise ValueError("original backup SHA-256 mismatch")
            if _sha(upgraded) != meta.get("updated_sha256"):
                raise ValueError("updated Parquet SHA-256 mismatch")

            before = pd.read_parquet(original)
            after = pd.read_parquet(upgraded)
            quarantined = meta.get("quality_policy") == "quarantined-original-zero-pairs-v1"
            if meta.get("quality_policy") is not None and not quarantined:
                raise ValueError("unknown OI quality policy")
            expected_columns = set(before.columns) | (
                {"oi_qty", "oi_valid"} if quarantined else {"oi_qty"}
            )
            if (
                "oi_qty" in before
                or "oi_valid" in before
                or set(after.columns) != expected_columns
                or len(before) != meta["records"]
                or len(after) != meta["records"]
            ):
                raise ValueError("original/updated schema or record count differs")
            # Preserve all original timestamps and notional values byte-for-byte
            # at the Parquet level, even when the original OI pair was zero.
            pd.testing.assert_frame_equal(
                after[list(before.columns)], before, check_exact=True
            )
            crash_data.validate_oi_quantity(after)
            if quarantined:
                mask = ~after["oi_valid"].to_numpy(dtype=bool)
                if type(meta.get("quarantined_zero_pairs")) is not int or (
                    int(mask.sum()) != meta["quarantined_zero_pairs"]
                ):
                    raise ValueError("quarantined zero-pair count differs from manifest")
                stamps = [
                    pd.Timestamp(ts).isoformat()
                    for ts in after.loc[mask, "ts"]
                ]
                if stamps != meta.get("quarantine_timestamps"):
                    raise ValueError("quarantine timestamps differ from manifest")
                if not mask.any():
                    raise ValueError("empty zero-pair quarantine marker")
            elif "quarantined_zero_pairs" in meta or "quarantine_timestamps" in meta:
                raise ValueError("quarantine metadata lacks explicit quality policy")

            verified += 1
            records += len(after)
            verified_names.append(name)
        except Exception as exc:
            # pyarrow corruption exceptions are not all ValueError subclasses.
            # Malformed files must produce an audit failure, not a traceback
            # or a falsely clean report.
            issues.append(f"{name}: {type(exc).__name__}: {exc}")

    # An orphan backup implies an aborted attempt; flag it instead of
    # claiming the full migration history has been verified.
    for backup in backups.glob("*.parquet"):
        if backup.name not in names:
            issues.append(f"{backup.name}: backup lacks transaction manifest")
    for partial in backups.glob("*.partial"):
        issues.append(f"{partial.name}: incomplete backup copy")
    for tmp in sources.glob("*.oi-quantity.tmp"):
        try:
            if date.fromisoformat(tmp.name.split(".parquet", 1)[0]) < boundary:
                issues.append(f"{tmp.name}: interrupted pre-holdout temporary file")
        except ValueError:
            issues.append(f"{tmp.name}: unrecognized temporary file")

    # The legacy inventory helper does not open any post-boundary file.
    # A corrupted migrated Parquet may fail here; retain the integrity
    # failure report rather than discarding it with an uncaught Arrow error.
    try:
        legacy_remaining = len(crash_data.legacy_oi_paths(root, symbol, before=boundary))
    except Exception as exc:
        issues.append(f"legacy inventory unavailable: {type(exc).__name__}: {exc}")
        legacy_remaining = None
    return {
        "ok": not issues,
        "symbol": symbol,
        "holdout_start": boundary.isoformat(),
        "verified_files": verified,
        "verified_records": records,
        "legacy_remaining": legacy_remaining,
        "first_verified": min(verified_names) if verified_names else None,
        "last_verified": max(verified_names) if verified_names else None,
        "issues": issues,
        "read_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read-only SHA-256, schema and content audit of historical OI migrations"
    )
    p.add_argument("--config", required=True, type=Path, help="original research lab.toml")
    p.add_argument("--symbol", default="BTCUSDT")
    args = p.parse_args(argv)
    try:
        result = audit_migrations(args.config, args.symbol)
    except (ValueError, OSError, RuntimeError) as exc:
        p.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
