"""Bounded, two-phase historical OI quarantine with explicit content pins.

Phase 1: inspect every named day in memory without modifying files.
Phase 2 (only with --apply): migrate one named day transactionally, then run
the independent migration auditor before advancing to the next day.
A failed check stops the sequence. The protected holdout is never opened.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, timedelta
from pathlib import Path

from lab.config import load_config
from lab.crash_oi_audit import audit_migrations
from lab.crash_oi_migration import _holdout_boundary
from lab.crash_oi_quarantine import quarantine_day
from lab.market_symbols import SYMBOL_RE

MAX_EXCEPTION_DAYS = 6
_DAY_KEYS = frozenset({"day", "archive_sha256", "legacy_sha256", "expect_zero_pairs"})


def load_pinned_plan(plan_path: Path, *, holdout_start: date) -> dict:
    """Reject malformed, overly broad or holdout-reaching plans *before* IO."""
    if plan_path.is_symlink() or not plan_path.is_file():
        raise ValueError(f"unsafe or missing pin manifest: {plan_path}")
    with plan_path.open(encoding="utf-8") as stream:
        plan = json.load(stream)
    if (
        not isinstance(plan, dict)
        or set(plan) != {"schema_version", "symbol", "holdout_start", "entries"}
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
    ):
        raise ValueError("unsupported or malformed historical OI pin manifest")
    symbol = plan["symbol"]
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid OI pin manifest symbol")
    if plan["holdout_start"] != holdout_start.isoformat():
        raise ValueError("holdout mismatch: refusing to inspect or modify any files")
    entries = plan["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_EXCEPTION_DAYS:
        raise ValueError(f"pin manifest must select 1-{MAX_EXCEPTION_DAYS} days")
    prev = None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _DAY_KEYS:
            raise ValueError("invalid OI quarantine day record")
        day = entry["day"]
        if not isinstance(day, str):
            raise ValueError("day must be YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError("day must be a calendar-valid YYYY-MM-DD") from exc
        if day != parsed.isoformat() or parsed >= holdout_start:
            raise ValueError(f"invalid, unnormalized or protected holdout day: {day}")
        if prev is not None and parsed != prev + timedelta(days=1):
            raise ValueError("pin manifest days must be consecutive, ordered and unique")
        prev = parsed
        for name in ("archive_sha256", "legacy_sha256"):
            if not isinstance(entry[name], str) or not re.fullmatch("[0-9a-f]{64}", entry[name]):
                raise ValueError(f"{day}: invalid {name}")
        n = entry["expect_zero_pairs"]
        if type(n) is not int or not 1 <= n <= 288:
            raise ValueError(f"{day}: invalid exact zero-pair count")
    return plan


def execute_pinned_plan(
    config_path: Path,
    plan_path: Path,
    *,
    apply: bool = False,
    get=None,
) -> dict:
    """Preflight the *entire* plan before any write, then audit each commit.

    Individual daily commits are durable, not a six-day atomic transaction.
    Any failure reports the offending day and the committed prefix.
    """
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    plan = load_pinned_plan(plan_path, holdout_start=boundary)
    symbol = plan["symbol"]
    entries = plan["entries"]
    pins = []
    for entry in entries:
        pins.append(
            {
                "config_path": config_path,
                "symbol": symbol,
                "day": entry["day"],
                "archive_sha256": entry["archive_sha256"],
                "legacy_sha256": entry["legacy_sha256"],
                "expect_zero_pairs": entry["expect_zero_pairs"],
            }
        )
    if get is not None:
        for pin in pins:
            pin["get"] = get

    # Critical: no applied mutation until every listed day has separately
    # passed exact local hash, remote ZIP hash, timestamp, notional and
    # (0,0)-pair mask checks.
    inspected = []
    for pin in pins:
        try:
            result = quarantine_day(**pin, apply=False)
        except Exception as exc:
            raise ValueError(
                f"read-only preflight failed at {pin['day']}; zero days modified: {type(exc).__name__}: {exc}"
            ) from exc
        if not result.get("eligible_for_explicit_quarantine"):
            raise ValueError(f"{pin['day']} did not qualify for explicit quarantine")
        inspected.append(
            {
                "day": pin["day"],
                "zero_pairs": result["quarantine_zero_pairs"],
                "validated_archive_sha256": result["original_archive_sha256"],
                "validated_legacy_sha256": result["original_legacy_sha256"],
            }
        )

    if not apply:
        return {
            "read_only": True,
            "preflight_ok": True,
            "symbol": symbol,
            "holdout_start": boundary.isoformat(),
            "checked_days": len(inspected),
            "total_zero_pairs": sum(x["zero_pairs"] for x in inspected),
            "days": inspected,
        }

    committed = []
    for pin in pins:
        try:
            result = quarantine_day(**pin, apply=True)
            if result.get("result") != "migrated":
                raise ValueError(f"unexpected single-day result: {result.get('result')}")
            committed.append(pin["day"])
            report = audit_migrations(config_path, symbol)
            if not report["ok"]:
                raise ValueError("post-commit independent audit failed: " + "; ".join(report["issues"]))
        except Exception as exc:
            raise ValueError(
                f"batch stopped at {pin['day']} after {len(committed)} "
                f"committed day(s): {', '.join(committed) or 'none'}; "
                f"cause: {type(exc).__name__}: {exc}"
            ) from exc

    return {
        "read_only": False,
        "preflight_ok": True,
        "symbol": symbol,
        "holdout_start": boundary.isoformat(),
        "committed_days": committed,
        "quarantined_zero_pairs": sum(x["zero_pairs"] for x in inspected),
        "migration_integrity_ok": True,
        "verified_files": report["verified_files"],
        "verified_records": report["verified_records"],
        "legacy_remaining": report["legacy_remaining"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preflight hash-pinned pre-holdout OI zero pairs; --apply audits each daily commit"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute_pinned_plan(args.config, args.manifest, apply=args.apply)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
