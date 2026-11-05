"""Bounded, read-only Binance OI source anomaly survey, pre-holdout only.

The default scan inspects at most 20 sequential daily legacy caches without
creating backups, locks or transaction manifests. Invalid (0,0) pairs can
become a *candidate* exception plan, never an automatic authorization.
An explicitly supplied --manifest-out only writes a NEW JSON plan file and
does not touch market-data caches. Existing files are never overwritten.
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
from lab.crash_oi_forensics import inspect_source
from lab.crash_oi_migration import _holdout_boundary, _quantity_from_original
from lab.crash_oi_quarantine_batch import MAX_EXCEPTION_DAYS, load_pinned_plan
from lab.market_symbols import SYMBOL_RE

MAX_SURVEY_DAYS = 20


def source_survey(
    config_path: Path,
    *,
    symbol: str = "BTCUSDT",
    start: str,
    days: int = 10,
    get=crash_data.get_bytes,
) -> dict:
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be an uppercase perpetual symbol")
    if type(days) is not int or not 1 <= days <= MAX_SURVEY_DAYS:
        raise ValueError(f"survey days must be between 1 and {MAX_SURVEY_DAYS}")
    try:
        day0 = date.fromisoformat(start)
    except (ValueError, TypeError) as exc:
        raise ValueError("start must be YYYY-MM-DD") from exc
    if start != day0.isoformat():
        raise ValueError("start must be YYYY-MM-DD")

    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    final_day = day0 + timedelta(days=days - 1)
    if day0 >= boundary or final_day >= boundary:
        raise ValueError("survey range touches protected holdout")
    root = cfg.data_dir / "crash"
    folder = root / "oi" / symbol
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"unsafe or missing source folder: {folder}")

    rows: list[dict] = []
    candidates: list[dict] = []
    for offset in range(days):
        day = (day0 + timedelta(days=offset)).isoformat()
        source = folder / f"{day}.parquet"
        # Explicitly selected range, never scan beyond the range or holdout.
        if source.is_symlink() or not source.is_file():
            rows.append({"day": day, "status": "blocked", "reason": "missing or unsafe original source"})
            continue

        try:
            original = pd.read_parquet(source)
            if "oi_qty" in original or "oi_valid" in original:
                raise ValueError("day is already migrated; survey accepts legacy caches only")

            downloaded: dict[str, bytes] = {}

            def fetch(url: str, cache: dict[str, bytes] = downloaded) -> bytes:
                if url not in cache:
                    cache[url] = get(url)
                return cache[url]

            r = inspect_source(config_path, symbol, day=day, get=fetch)
            if r["archive_rows"] != r["local_rows"] or not (
                r["timestamp_set_match"] and r["notional_matches_legacy"]
            ):
                raise ValueError("archive timestamps, row count or notionals differ from original cache")
            blob = downloaded[crash_data.url_of("oi", symbol, day)]
            qbad = r["nonpositive_or_nonfinite_quantity"]
            vbad = r["nonpositive_or_nonfinite_notional"]

            if not qbad and not vbad:
                # Ordinary parser must approve *all* observations before
                # classifying an archive as clean.
                parsed = crash_data.parse_metrics(blob)
                if len(parsed) != r["local_rows"]:
                    raise ValueError("clean parser row count mismatch")
                rows.append({
                    "day": day, "status": "clean", "rows": r["archive_rows"],
                    "archive_sha256": r["archive_sha256"],
                    "legacy_sha256": r["legacy_file_sha256"],
                })
                continue

            # Strictly verify every ZIP row and the original notional series;
            # equal per-column nonpositive counts alone are insufficient.
            upgraded, n = _quantity_from_original(
                source, blob, quarantine_zero_pairs=True
            )
            invalid = ~upgraded["oi_valid"].to_numpy(dtype=bool)
            quantity = pd.to_numeric(upgraded["oi_qty"], errors="raise").to_numpy(dtype=float)
            if n != r["local_rows"] or not np.array_equal(invalid, np.isnan(quantity)):
                raise ValueError("invalid quality-mask alignment or source record count")
            count = int(invalid.sum())
            if count != qbad or count != vbad:
                raise ValueError("zero-pair count inconsistent with forensic scan")
            pin = {
                "day": day,
                "archive_sha256": r["archive_sha256"],
                "legacy_sha256": r["legacy_file_sha256"],
                "expect_zero_pairs": count,
            }
            candidates.append(pin)
            rows.append({
                "day": day, "status": "exact_zero_pairs",
                "rows": n, "unavailable_quantity": count,
                "archive_sha256": r["archive_sha256"],
                "legacy_sha256": r["legacy_file_sha256"],
            })
        except Exception as exc:
            rows.append({
                "day": day, "status": "blocked",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    blocked = [x["day"] for x in rows if x["status"] == "blocked"]
    clean = [x["day"] for x in rows if x["status"] == "clean"]
    plan = None
    if not blocked and not clean and 1 <= len(candidates) <= MAX_EXCEPTION_DAYS:
        plan = {
            "schema_version": 1,
            "symbol": symbol,
            "holdout_start": boundary.isoformat(),
            "entries": candidates,
        }

    return {
        "read_only": True,
        "symbol": symbol,
        "holdout_start": boundary.isoformat(),
        "start": start,
        "days_requested": days,
        "days_scanned": len(rows),
        "clean_days": len(clean),
        "candidate_days": len(candidates),
        "invalid_zero_pairs": sum(x["expect_zero_pairs"] for x in candidates),
        "blocked_days": blocked,
        "observations": rows,
        "candidate_manifest": plan,
        "ready_for_manual_review": bool(plan is not None),
    }


def write_new_manifest(path: Path, report: dict) -> None:
    """Opt-in plan artifact only; never overwrite a plan or cached source."""
    plan = report["candidate_manifest"]
    if not plan:
        raise ValueError(
            "cannot write a plan: select a fully verified consecutive group "
            f"of 1-{MAX_EXCEPTION_DAYS} anomalous days with no clean/blocked days"
        )
    if path.is_symlink() or path.exists():
        raise ValueError(f"refusing to overwrite manifest path: {path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError(f"manifest destination parent missing or unsafe: {path.parent}")
    # Validate using the exact parser that the subsequent apply action uses.
    # The candidate plan itself is generated only from all-row source checks.
    contents = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(contents)
        stream.flush()
    try:
        load_pinned_plan(path, holdout_start=date.fromisoformat(report["holdout_start"]))
    except Exception:
        # Never silently alter the cache; a malformed plan must be removed
        # so it cannot be mistakenly presented as an approved artifact.
        path.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read-only survey of at most 20 pre-holdout legacy OI daily ZIPs"
    )
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", required=True, help="first UTC day (YYYY-MM-DD)")
    p.add_argument("--days", type=int, default=10)
    p.add_argument(
        "--manifest-out", type=Path,
        help="EXPLICIT opt-in: create a NEW plan file only when all days are exact zero-pair candidates",
    )
    a = p.parse_args(argv)
    try:
        report = source_survey(
            a.config, symbol=a.symbol, start=a.start, days=a.days
        )
        if a.manifest_out:
            write_new_manifest(a.manifest_out, report)
            report["manifest_created"] = str(a.manifest_out)
            report["read_only"] = False  # only the newly requested manifest file
    except (ValueError, OSError, RuntimeError) as exc:
        p.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if report["blocked_days"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
