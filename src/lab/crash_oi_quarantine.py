"""Explicit single-day Binance OI zero-pair quarantine; default is read-only.

No implicit downgrade of ordinary OI validation. The operator must name
the day, original ZIP SHA-256, original Parquet SHA-256 and exact count of
(0 quantity, 0 notional) snapshots. Matched anomalies become missing
quantity with an audited boolean quality mask; original notionals remain
unaltered. Never opens holdout files.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from lab import crash_data
from lab.config import load_config
from lab.crash_oi_forensics import inspect_source
from lab.crash_oi_migration import (
    MIN_FREE_MIB,
    _headroom,
    _holdout_boundary,
    _migrate_one,
    _quantity_from_original,
    _reconcile_prior_transactions,
    _sha,
)
from lab.market_symbols import SYMBOL_RE
from lab.persistence import file_lock


def quarantine_day(
    config_path: Path,
    symbol: str,
    day: str,
    *,
    archive_sha256: str,
    legacy_sha256: str,
    expect_zero_pairs: int,
    apply: bool = False,
    get=crash_data.get_bytes,
) -> dict:
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be uppercase perpetual symbol")
    try:
        parsed_day = date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("day must be a valid YYYY-MM-DD date") from exc
    if day != parsed_day.isoformat():
        raise ValueError("day must be YYYY-MM-DD")
    for label, digest in (("archive", archive_sha256), ("legacy", legacy_sha256)):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"{label} SHA-256 must be 64 lowercase hex characters")
    if not isinstance(expect_zero_pairs, int) or not 1 <= expect_zero_pairs <= 288:
        raise ValueError("expected zero-pair count must be in 1..288")

    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    if parsed_day >= boundary:
        raise ValueError("cannot quarantine OI from protected holdout")
    root = cfg.data_dir / "crash"
    folder = root / "oi" / symbol
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError(f"missing/unsafe source folder: {folder}")
    source = folder / f"{day}.parquet"
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"missing/unsafe pre-holdout file: {source}")
    backup_dir = root / "oi-original-backups" / symbol
    lock = folder / ".oi-quantity-migration.lock"

    def perform() -> dict:
        # Capture exactly one download, then pass the identical bytes to the
        # transactional migration. The original ZIP hash is pinned by input.
        captured: dict[str, bytes] = {}

        def fetch(url: str) -> bytes:
            if url not in captured:
                captured[url] = get(url)
            return captured[url]

        original_digest = _sha(source)
        if original_digest != legacy_sha256:
            raise ValueError(f"local legacy Parquet SHA-256 changed: {source}")
        report = inspect_source(config_path, symbol, day=day, get=fetch)
        if report["archive_sha256"] != archive_sha256:
            raise ValueError(f"original ZIP SHA-256 differs from pinned value for {day}")
        if report["legacy_file_sha256"] != legacy_sha256:
            raise ValueError(f"legacy file changed during forensic inspection for {day}")
        if not report["timestamp_set_match"] or not report["notional_matches_legacy"]:
            raise ValueError("original ZIP timestamps or notional OI differ from legacy cache")
        if (
            report["nonpositive_or_nonfinite_quantity"] != expect_zero_pairs
            or report["nonpositive_or_nonfinite_notional"] != expect_zero_pairs
        ):
            raise ValueError("unexpected number of nonpositive/nonfinite source observations")
        # Enforce matching (0,0) pairs, exact row alignment and the quality
        # schema in memory even for the read-only dry run.
        archive_url = crash_data.url_of("oi", symbol, day)
        preview, _ = _quantity_from_original(source, captured[archive_url], quarantine_zero_pairs=True)
        exact_zeros = int((~preview["oi_valid"]).sum())
        if exact_zeros != expect_zero_pairs:
            raise ValueError(f"quarantine mask count differs: {exact_zeros}")
        if not apply:
            return {
                "read_only": True,
                "dry_run": True,
                "day": day,
                "original_archive_sha256": archive_sha256,
                "original_legacy_sha256": legacy_sha256,
                "quarantine_zero_pairs": expect_zero_pairs,
                "timestamp_set_match": True,
                "notional_matches_legacy": True,
                "eligible_for_explicit_quarantine": True,
            }
        result = _migrate_one(
            source,
            backup_dir / source.name,
            symbol=symbol,
            boundary=boundary,
            get=fetch,
            quarantine_archive_sha256=archive_sha256,
            expected_zero_pairs=expect_zero_pairs,
        )
        if result != "migrated":
            raise ValueError(f"expected one explicit migrated day, got: {result}")
        return {
            "read_only": False,
            "dry_run": False,
            "day": day,
            "result": result,
            "quarantined_zero_pairs": expect_zero_pairs,
            "original_archive_sha256": archive_sha256,
            "original_legacy_sha256": legacy_sha256,
            "backup_path": str(backup_dir / source.name),
            "manifest_path": str(backup_dir / (source.name + ".migration.json")),
        }

    if not apply:
        # Strictly no on-disk changes, including no lock or transaction file.
        return perform()
    with file_lock(lock):
        _headroom(folder, MIN_FREE_MIB)
        _reconcile_prior_transactions(folder, backup_dir, symbol=symbol, boundary=boundary)
        return perform()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly quarantine exact-zero OI pairs in one verified pre-holdout archive"
    )
    parser.add_argument("--config", required=True, type=Path, help="original research checkout lab.toml")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--legacy-sha256", required=True)
    parser.add_argument("--expect-zero-pairs", type=int, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="write verified backup and explicit quality-mask transaction"
    )
    a = parser.parse_args(argv)
    try:
        result = quarantine_day(
            a.config,
            a.symbol,
            a.day,
            archive_sha256=a.archive_sha256,
            legacy_sha256=a.legacy_sha256,
            expect_zero_pairs=a.expect_zero_pairs,
            apply=a.apply,
        )
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
