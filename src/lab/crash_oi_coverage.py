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

from lab.config import load_config
from lab.crash_oi_migration import _holdout_boundary
from lab.market_symbols import SYMBOL_RE
from lab.persistence import read_json

INTERVAL = pd.Timedelta(minutes=5)
DAILY_EXPECTED = 24 * 60 // 5


def _cadence(frame: pd.DataFrame, day: date) -> dict:
    if "ts" not in frame:
        raise ValueError("missing timestamp column")
    ts = pd.DatetimeIndex(pd.to_datetime(frame["ts"], utc=True, errors="raise"))
    if ts.isna().any():
        raise ValueError("missing or invalid timestamp")
    lo = pd.Timestamp(day, tz="UTC")
    hi = lo + pd.Timedelta(days=1)
    if ((ts < lo) | (ts > hi)).any():
        raise ValueError("snapshot timestamp outside named UTC day")
    if len(ts) < 1:
        raise ValueError("empty OI period")

    diffs = np.diff(ts.asi8)
    step = INTERVAL.value
    duplicate_or_unordered = int(np.count_nonzero(diffs <= 0))
    irregular = int(np.count_nonzero(diffs % step != 0))
    # Only positive, aligned jumps can be counted as definitely missing
    # five-minute observations. Boundary shortfalls are not inferred from
    # file timestamp labels, since archives may include the next 00:00.
    internal_missing = int(sum(max(0, int(x // step) - 1) for x in diffs if x > step and x % step == 0))
    shortfall = max(0, DAILY_EXPECTED - len(ts))
    extra = max(0, len(ts) - DAILY_EXPECTED)
    abnormal = bool(shortfall or extra or duplicate_or_unordered or irregular)
    summary = {
        "day": day.isoformat(),
        "observations": int(len(ts)),
        "benchmark_per_full_day": DAILY_EXPECTED,
        "count_shortfall": shortfall,
        "count_excess": extra,
        "internal_missing_slots": internal_missing,
        "unordered_or_duplicate": duplicate_or_unordered,
        "non_five_minute_intervals": irregular,
        "first_timestamp": ts[0].isoformat(),
        "last_timestamp": ts[-1].isoformat(),
    }
    if not abnormal and internal_missing:
        abnormal = True
    summary["abnormal"] = abnormal
    return summary


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
            frame = pd.read_parquet(p, columns=["ts"])
            item = _cadence(frame, day)
            observed += item["observations"]
            benchmark += DAILY_EXPECTED
            checked += 1
            day_names.append(day)
            if item["abnormal"]:
                anomaly_days.append(item)
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
        "missing_calendar_days_within_checked_range": missing_days,
        "errors": errors,
        "coverage_ok_for_checked_days": not (anomaly_days or missing_days or errors) and checked > 0,
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
