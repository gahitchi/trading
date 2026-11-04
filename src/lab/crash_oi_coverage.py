"""Read-only five-minute OI snapshot coverage diagnostics for migrated research days.

Migration integrity and time-series completeness are different gates: this
tool does not modify files and does not certify trading signals. Protected
holdout files are never opened, even if a corrupt manifest names them.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from lab import crash_data
from lab.config import load_config
from lab.crash_oi_migration import _holdout_boundary
from lab.market_symbols import SYMBOL_RE
from lab.persistence import read_json

INTERVAL = pd.Timedelta(minutes=5)
DAILY_EXPECTED = 24 * 60 // 5


def _missing_runs(missing: pd.DatetimeIndex) -> list[dict]:
    """Compact chronological missing intervals; count independent of raw row order."""
    if len(missing) == 0:
        return []
    ranges = []
    first = previous = missing[0]
    n = 1
    for value in missing[1:]:
        if value - previous == INTERVAL:
            previous = value
            n += 1
        else:
            ranges.append({"start": first.isoformat(), "end": previous.isoformat(), "slots": n})
            first = previous = value
            n = 1
    ranges.append({"start": first.isoformat(), "end": previous.isoformat(), "slots": n})
    return ranges


def _cadence(frame: pd.DataFrame, day: date) -> dict:
    """Assess actual UTC five-minute slots, independently of archived row order.

    Some archives use period-start (00:00..23:55), others period-end
    (00:05..next 00:00) timestamps. Compare both complete grids and
    choose the convention needing the fewest missing/extra slots.
    """
    if "ts" not in frame:
        raise ValueError("missing timestamp column")
    ts = pd.DatetimeIndex(pd.to_datetime(frame["ts"], utc=True, errors="raise"))
    if ts.isna().any():
        raise ValueError("missing or invalid timestamp")
    if not len(ts):
        raise ValueError("empty OI period")
    lo = pd.Timestamp(day, tz="UTC")
    hi = lo + pd.Timedelta(days=1)
    if ((ts < lo) | (ts > hi)).any():
        raise ValueError("snapshot timestamp outside named UTC day")

    # Raw file order is only a warning: the research loader sorts by ts.
    inversions = int(np.count_nonzero(np.diff(ts.asi8) < 0))
    unique = ts.unique().sort_values()
    duplicates = len(ts) - len(unique)
    start_grid = pd.date_range(lo, periods=DAILY_EXPECTED, freq=INTERVAL)
    end_grid = pd.date_range(lo + INTERVAL, periods=DAILY_EXPECTED, freq=INTERVAL)
    options = [
        (grid.difference(unique), unique.difference(grid), label)
        for grid, label in ((start_grid, "period-start"), (end_grid, "period-end"))
    ]
    # Tie-break toward period-start, never toward the original unsorted ordering.
    missing, unexpected, convention = min(
        options, key=lambda x: len(x[0]) + len(x[1])
    )
    first, last = unique[0], unique[-1]
    internal = int(((missing > first) & (missing < last)).sum())
    leading = int((missing < first).sum())
    trailing = int((missing > last).sum())
    off_cadence = int(np.count_nonzero((ts.asi8 - lo.value) % INTERVAL.value))
    failures = bool(len(missing) or len(unexpected) or duplicates or off_cadence)
    return {
        "day": day.isoformat(),
        "observations": len(ts),
        "unique_observations": len(unique),
        "benchmark_per_full_day": DAILY_EXPECTED,
        "timestamp_convention": convention,
        "count_shortfall": max(0, DAILY_EXPECTED - len(ts)),
        "count_excess": max(0, len(ts) - DAILY_EXPECTED),
        "missing_slots": len(missing),
        "missing_ranges": _missing_runs(missing),
        "unexpected_slots": len(unexpected),
        "duplicate_timestamps": duplicates,
        "raw_order_inversions": inversions,
        "off_five_minute_grid": off_cadence,
        "internal_missing_slots": internal,
        "leading_missing_slots": leading,
        "trailing_missing_slots": trailing,
        "first_timestamp": first.isoformat(),
        "last_timestamp": last.isoformat(),
        "abnormal": failures,
        "ordering_only": bool(inversions) and not failures,
    }


def audit_coverage(config_path: Path, symbol: str = "BTCUSDT") -> dict:
    """Report migrated pre-holdout days; does not touch historical holdout."""
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be an uppercase perpetual symbol")
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    root = cfg.data_dir / "crash"
    folder = root / "oi" / symbol
    backups = root / "oi-original-backups" / symbol
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"unsafe or missing OI cache directory: {folder}")
    if backups.is_symlink() or (backups.exists() and not backups.is_dir()):
        raise ValueError(f"unsafe backup directory: {backups}")

    checked = 0
    observed = 0
    benchmark = 0
    anomaly_days = []
    ordering_only_days = []
    quality_anomaly_days = []
    errors = []
    day_names: list[date] = []
    for manifest in sorted(backups.glob("*.parquet.migration.json")):
        name = manifest.name.removesuffix(".migration.json")
        try:
            day = date.fromisoformat(Path(name).stem)
            if day >= boundary:
                raise ValueError("manifest targets protected holdout")
            if manifest.is_symlink() or name != day.isoformat() + ".parquet":
                raise ValueError("unsafe or invalid migration manifest")
            meta = read_json(manifest)
            if meta.get("source_name") != name or meta.get("symbol") != symbol or meta.get("status") != "committed":
                raise ValueError("migration not committed or manifest identity differs")
            p = folder / name
            if p.is_symlink() or not p.is_file():
                raise ValueError("missing or unsafe upgraded OI file")
            frame = pd.read_parquet(p)
            item = _cadence(frame, day)
            if "oi_valid" in frame:
                crash_data.validate_oi_quantity(frame)
                invalid = ~frame["oi_valid"].to_numpy(dtype=bool)
                if invalid.any():
                    quality_anomaly_days.append({
                        "day": day.isoformat(),
                        "unavailable_quantity_snapshots": int(invalid.sum()),
                        "timestamps": [
                            pd.Timestamp(ts).isoformat()
                            for ts in frame.loc[invalid, "ts"]
                        ],
                    })
            observed += item["observations"]
            benchmark += DAILY_EXPECTED
            checked += 1
            day_names.append(day)
            if item["abnormal"]:
                anomaly_days.append(item)
            elif item["ordering_only"]:
                ordering_only_days.append({
                    "day": day.isoformat(),
                    "raw_order_inversions": item["raw_order_inversions"],
                    "unique_observations": item["unique_observations"],
                    "timestamp_convention": item["timestamp_convention"],
                })
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    # A missing day between two migrated days is separate from holes
    # inside a particular day's file. Do not inspect that day's contents.
    missing_days = []
    for before, after in zip(day_names, day_names[1:]):
        d = before + timedelta(days=1)
        while d < after:
            missing_days.append(d.isoformat())
            d += timedelta(days=1)
    return {
        "read_only": True,
        "scope": "migrated_pre_holdout_files_only",
        "integrity_verified": False,
        "symbol": symbol,
        "holdout_start": boundary.isoformat(),
        "checked_days": checked,
        "observations": observed,
        "full_day_count_benchmark": benchmark,
        "count_shortfall": benchmark - observed,
        "abnormal_days": anomaly_days,
        "ordering_only_days": ordering_only_days,
        "ordering_only_day_count": len(ordering_only_days),
        "quality_anomaly_days": quality_anomaly_days,
        "missing_calendar_days_within_checked_range": missing_days,
        "errors": errors,
        "coverage_ok_for_checked_days": not (
            anomaly_days or quality_anomaly_days or missing_days or errors
        ) and checked > 0,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only five-minute OI coverage check for migrated research days")
    p.add_argument("--config", type=Path, required=True, help="original research checkout's lab.toml")
    p.add_argument("--symbol", default="BTCUSDT")
    args = p.parse_args(argv)
    try:
        result = audit_coverage(args.config, args.symbol)
    except (OSError, ValueError, RuntimeError) as exc:
        p.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["coverage_ok_for_checked_days"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
