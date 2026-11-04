"""Read-only source diagnostics for a blocked historical Binance OI migration.

Retrieves at most one original daily metrics ZIP into memory, never writes it.
Checks the exact pre-existing holdout partition and never opens holdout cache.
Diagnostics do NOT authorize accepting nonpositive or mismatched OI values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from lab import crash_data
from lab.config import load_config
from lab.crash_oi_migration import _holdout_boundary, _sha
from lab.market_symbols import SYMBOL_RE


def inspect_source(
    config_path: Path,
    symbol: str = "BTCUSDT",
    *,
    day: str | None = None,
    get=crash_data.get_bytes,
) -> dict:
    """Inspect the next legacy daily file or a named pre-holdout day.

    No cache, lock, transaction, archive ZIP or result is written. Public
    archive data is downloaded to memory only, without retrying the 20-day
    migration. Zero values are reported, not reinterpreted or substituted.
    """
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be an uppercase perpetual symbol")
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    root = cfg.data_dir / "crash"
    folder = root / "oi" / symbol
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"unsafe or missing OI directory: {folder}")
    if day is None:
        eligible = crash_data.legacy_oi_paths(root, symbol, boundary)
        if not eligible:
            raise ValueError("no remaining legacy pre-holdout daily OI caches")
        source = eligible[0]
    else:
        parsed_day = date.fromisoformat(day)
        if parsed_day >= boundary:
            raise ValueError("refusing to inspect protected holdout period")
        if day != parsed_day.isoformat():
            raise ValueError("day must be YYYY-MM-DD")
        source = folder / f"{day}.parquet"
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"unsafe or missing pre-holdout legacy file: {source}")
    if date.fromisoformat(source.stem) >= boundary:
        raise ValueError("refusing to inspect protected holdout file")

    old = pd.read_parquet(source)
    if not {"ts", "oi_value"}.issubset(old.columns):
        raise ValueError("existing cache is missing timestamp or notional column")
    url = crash_data.url_of("oi", symbol, source.stem)
    blob = get(url)
    archive = crash_data._csv(blob)
    required = {"create_time", "sum_open_interest", "sum_open_interest_value"}
    if not required.issubset(archive.columns):
        raise ValueError(f"original archive missing columns: {sorted(required - set(archive.columns))}")

    qty = pd.to_numeric(archive["sum_open_interest"], errors="coerce").to_numpy(dtype=float)
    notional = pd.to_numeric(archive["sum_open_interest_value"], errors="coerce").to_numpy(dtype=float)
    qty_bad = ~np.isfinite(qty) | (qty <= 0)
    value_bad = ~np.isfinite(notional) | (notional <= 0)

    # Compare the archive to existing cache irrespective of the positivity
    # policy, using the same timestamp alignment as the actual migration.
    source_times = pd.to_datetime(archive["create_time"], utc=True, errors="coerce")
    old_times = pd.to_datetime(old["ts"], utc=True, errors="coerce")
    keys_ok = bool(
        len(archive) == len(old)
        and not source_times.isna().any()
        and not old_times.isna().any()
        and not source_times.duplicated().any()
        and not old_times.duplicated().any()
    )
    timestamp_set_match = False
    matching_notional = False
    if keys_ok:
        timestamp_set_match = bool(
            np.array_equal(
                np.sort(source_times.to_numpy()),
                np.sort(old_times.to_numpy()),
            )
        )
        if timestamp_set_match:
            by_time = pd.Series(notional, index=pd.DatetimeIndex(source_times))
            aligned = by_time.reindex(pd.DatetimeIndex(old_times)).to_numpy(dtype=float)
            original_value = pd.to_numeric(old["oi_value"], errors="coerce").to_numpy(dtype=float)
            matching_notional = bool(np.array_equal(aligned, original_value))

    flagged = np.flatnonzero(qty_bad | value_bad)
    samples = []
    for idx in flagged[:12]:
        index = int(idx)
        samples.append({
            "archive_row": index,
            "timestamp": str(archive.iloc[index]["create_time"]),
            "quantity": str(archive.iloc[index]["sum_open_interest"]),
            "notional": str(archive.iloc[index]["sum_open_interest_value"]),
            "quantity_invalid": bool(qty_bad[index]),
            "notional_invalid": bool(value_bad[index]),
        })

    return {
        "read_only": True,
        "holdout_start": boundary.isoformat(),
        "symbol": symbol,
        "day": source.stem,
        "archive_url": url,
        "archive_sha256": hashlib.sha256(blob).hexdigest(),
        "legacy_file_sha256": _sha(source),
        "local_rows": len(old),
        "archive_rows": len(archive),
        "timestamp_set_match": timestamp_set_match,
        "notional_matches_legacy": matching_notional,
        "nonpositive_or_nonfinite_quantity": int(qty_bad.sum()),
        "nonpositive_or_nonfinite_notional": int(value_bad.sum()),
        "first_invalid_rows": samples,
        "safe_to_migrate_under_current_policy": bool(
            not flagged.size and timestamp_set_match and matching_notional
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only investigation of invalid Binance historical OI archives"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--day", help="one explicit pre-holdout date (default: first unmigrated day)")
    args = parser.parse_args(argv)
    try:
        result = inspect_source(args.config, args.symbol, day=args.day)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    # An expected source-quality block must not turn into a successful exit.
    return 0 if result["safe_to_migrate_under_current_policy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
