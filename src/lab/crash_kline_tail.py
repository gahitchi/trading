"""Explicit, source-pinned append-only recovery of pre-holdout daily candle archives.

A monthly candle archive spanning the holdout must NEVER be read to recover
the pre-holdout tail. Fetch just the individual days from the holdout's month,
strictly before the boundary. Survey is read-only by default; apply requires
a separate source-SHA-pinned JSON plan. The tool never changes existing
monthly Parquet, OI history, or protected holdout data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from lab import crash_data
from lab.config import load_config
from lab.crash_oi_migration import _holdout_boundary, _sha, _sync_directory
from lab.market_symbols import SYMBOL_RE
from lab.persistence import atomic_json, file_lock, read_json

PLAN_VERSION = 1
KIND = "kline-preholdout-daily-tail-v1"
MINUTE = pd.Timedelta(minutes=1)


def _days(boundary: date) -> list[date]:
    first = boundary.replace(day=1)
    if boundary == first:
        raise ValueError("no daily pre-holdout tail in the boundary month")
    days = [first + timedelta(days=n) for n in range((boundary - first).days)]
    if len(days) > 30:
        raise ValueError("daily recovery exceeds 30-day safety bound")
    return days


def _sources(cfg, symbol: str, *, create: bool = False) -> Path:
    root = cfg.data_dir / "crash"
    folder = root / "klines" / symbol
    for p in (cfg.data_dir, root, root / "klines", folder):
        if p.is_symlink():
            raise ValueError(f"unsafe symlink directory: {p}")
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise ValueError(f"missing candle cache directory: {folder}")
    return folder


def _daily_candles(blob: bytes, day: date) -> pd.DataFrame:
    frame = crash_data.parse_klines(blob)
    expected = pd.date_range(
        pd.Timestamp(day, tz="UTC") + MINUTE,
        periods=1440,
        freq=MINUTE,
    )
    observed = pd.DatetimeIndex(frame["ts"])
    if len(observed) != 1440 or not observed.equals(expected):
        raise ValueError(f"{day}: source candle timestamps are not exactly 1440 ordered one-minute bars")
    raw = frame[list(crash_data.KLINE_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(raw).all():
        raise ValueError(f"{day}: nonfinite candle input")
    o, h, lo, c, volume, taker = raw.T
    if (
        (o <= 0).any() or (h <= 0).any() or (lo <= 0).any()
        or (c <= 0).any() or (volume < 0).any() or (taker < 0).any()
        or (h < np.maximum(o, c)).any()
        or (lo > np.minimum(o, c)).any() or (lo > h).any()
    ):
        raise ValueError(f"{day}: invalid candle OHLC or volume")
    return frame


def _plan(symbol: str, boundary: date, get) -> tuple[dict, list[pd.DataFrame]]:
    items = []
    data = []
    for day in _days(boundary):
        url = crash_data.url_of("klines", symbol, day.isoformat())
        blob = get(url)
        frame = _daily_candles(blob, day)
        items.append({
            "day": day.isoformat(),
            "archive_sha256": hashlib.sha256(blob).hexdigest(),
            "rows": len(frame),
        })
        data.append(frame)
    return {
        "version": PLAN_VERSION,
        "kind": KIND,
        "symbol": symbol,
        "holdout_start": boundary.isoformat(),
        "archives": items,
    }, data


def _validate_plan(plan: dict, symbol: str, boundary: date) -> None:
    expected = [d.isoformat() for d in _days(boundary)]
    if not isinstance(plan, dict) or {
        "version", "kind", "symbol", "holdout_start", "archives"
    } != set(plan):
        raise ValueError("invalid daily tail source plan schema")
    if (
        plan["version"] != PLAN_VERSION or plan["kind"] != KIND
        or plan["symbol"] != symbol or plan["holdout_start"] != boundary.isoformat()
    ):
        raise ValueError("tail source plan differs from existing protected holdout")
    if not isinstance(plan["archives"], list) or len(plan["archives"]) != len(expected):
        raise ValueError("daily tail plan does not enumerate every pre-holdout day")
    if [v.get("day") for v in plan["archives"]] != expected:
        raise ValueError("daily tail plan includes missing, duplicate or protected dates")
    for item in plan["archives"]:
        if set(item) != {"day", "archive_sha256", "rows"} or item["rows"] != 1440:
            raise ValueError("invalid daily source manifest entry")
        raw = item["archive_sha256"]
        if not isinstance(raw, str) or len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
            raise ValueError("invalid daily source ZIP SHA-256")


def survey(config_path: Path, symbol: str = "BTCUSDT", *, get=None) -> dict:
    """Validate every original daily ZIP without writing to the cache."""
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid perpetual symbol")
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    _sources(cfg, symbol)
    plan, _ = _plan(symbol, boundary, get or crash_data.get_bytes)
    return plan


def _store(frame: pd.DataFrame, target: Path) -> str:
    """No-clobber atomic hard-link commit; source file is never overwritten."""
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".preholdout-", dir=target.parent)
    os.close(fd)
    tmp = Path(temporary)
    try:
        frame.to_parquet(tmp, index=False)
        verification = pd.read_parquet(tmp)
        if not verification.equals(frame):
            raise ValueError("daily candle temporary write differs from verified source")
        with tmp.open("rb") as inp:
            os.fsync(inp.fileno())
        digest = _sha(tmp)
        # The destination must not be replaced, even under a racing writer.
        os.link(tmp, target)
        _sync_directory(target.parent)
        return digest
    finally:
        tmp.unlink(missing_ok=True)


def _daily_manifest(plan: dict, item: dict, sha: str) -> dict:
    return {
        "kind": KIND,
        "symbol": plan["symbol"],
        "holdout_start": plan["holdout_start"],
        "day": item["day"],
        "archive_sha256": item["archive_sha256"],
        "parquet_sha256": sha,
        "rows": 1440,
    }


def _verify_existing(target: Path, manifest: Path, frame: pd.DataFrame, plan: dict, item: dict) -> bool:
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"unsafe existing candle cache: {target}")
    if not pd.read_parquet(target).equals(frame):
        raise ValueError(f"existing daily candle data differs from verified archive: {target}")
    expected = _daily_manifest(plan, item, _sha(target))
    if manifest.exists():
        if manifest.is_symlink() or read_json(manifest) != expected:
            raise ValueError(f"existing source-pin manifest mismatches: {manifest}")
        return True
    # Interrupted post-file/pre-manifest stage: reconcile only if the
    # exact entire decoded frame agrees with the hash-pinned source ZIP.
    atomic_json(manifest, expected)
    return False


def apply(config_path: Path, plan_path: Path, symbol: str = "BTCUSDT", *, get=None) -> dict:
    """Apply exact preflight source hashes, atomically, append-only."""
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid perpetual symbol")
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    folder = _sources(cfg, symbol)
    plan = read_json(plan_path)
    _validate_plan(plan, symbol, boundary)

    # Check every remote ZIP before committing ANY day.
    actual, frames = _plan(symbol, boundary, get or crash_data.get_bytes)
    if actual != plan:
        raise ValueError("daily source ZIP SHA-256 or coverage changed since preflight")
    results = []
    with file_lock(folder / ".kline-preholdout-tail.lock"):
        for item, frame in zip(plan["archives"], frames, strict=True):
            target = folder / (item["day"] + ".parquet")
            manifest = folder / (item["day"] + ".parquet.preholdout.json")
            if manifest.is_symlink() or (manifest.exists() and not target.exists()):
                raise ValueError(f"unsafe orphan daily source manifest: {manifest}")
            if target.exists() or target.is_symlink():
                existing = _verify_existing(target, manifest, frame, plan, item)
                results.append({"day": item["day"], "status": "verified-existing" if existing else "reconciled"})
                continue
            digest = _store(frame, target)
            atomic_json(manifest, _daily_manifest(plan, item, digest))
            results.append({"day": item["day"], "status": "committed"})
    return {"read_only": False, "holdout_start": boundary.isoformat(), "days": results}


def audit(config_path: Path, plan_path: Path, symbol: str = "BTCUSDT") -> dict:
    """Independent read-only audit of locally committed daily tail and sidecars."""
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    folder = _sources(cfg, symbol)
    plan = read_json(plan_path)
    _validate_plan(plan, symbol, boundary)
    issues = []
    for item in plan["archives"]:
        target = folder / (item["day"] + ".parquet")
        manifest = folder / (item["day"] + ".parquet.preholdout.json")
        try:
            if target.is_symlink() or manifest.is_symlink():
                raise ValueError("symlink daily cache or source manifest")
            if not target.is_file() or not manifest.is_file():
                raise ValueError("missing daily cache or source manifest")
            expected = _daily_manifest(plan, item, _sha(target))
            if read_json(manifest) != expected:
                raise ValueError("daily manifest content or file SHA differs")
            frame = pd.read_parquet(target)
            # Validate all columns as well as exact UTC timestamps.
            expected_cols = ["ts", *crash_data.KLINE_COLUMNS]
            if list(frame) != expected_cols:
                raise ValueError("daily kline columns differ from Binance parser")
            _daily_candles_from_frame(frame, date.fromisoformat(item["day"]))
        except Exception as exc:
            issues.append(f"{item['day']}: {type(exc).__name__}: {exc}")
    return {
        "read_only": True,
        "ok": not issues,
        "checked_days": len(plan["archives"]),
        "holdout_start": boundary.isoformat(),
        "issues": issues,
    }


def _daily_candles_from_frame(frame: pd.DataFrame, day: date) -> None:
    expected = pd.date_range(
        pd.Timestamp(day, tz="UTC") + MINUTE,
        periods=1440, freq=MINUTE,
    )
    ts = pd.DatetimeIndex(frame["ts"])
    if not ts.equals(expected):
        raise ValueError("daily candle timestamps altered")
    raw = frame[list(crash_data.KLINE_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(raw).all():
        raise ValueError("nonfinite candle cache values")
    o, h, lo, c, v, taker = raw.T
    if (
        (o <= 0).any() or (h <= 0).any() or (lo <= 0).any()
        or (c <= 0).any() or (v < 0).any() or (taker < 0).any()
        or (h < np.maximum(o, c)).any()
        or (lo > np.minimum(o, c)).any() or (lo > h).any()
    ):
        raise ValueError("invalid candle source values")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append-only source-pinned pre-holdout daily candle tail")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args(argv)
    if (args.apply and args.audit) or (args.plan_out and (args.apply or args.audit)):
        parser.error("select exactly one of preflight, apply, and audit")
    try:
        if args.apply:
            if args.plan is None:
                parser.error("--apply requires --plan with pinned source hashes")
            report = apply(args.config, args.plan, args.symbol)
        elif args.audit:
            if args.plan is None:
                parser.error("--audit requires --plan")
            report = audit(args.config, args.plan, args.symbol)
        else:
            if args.plan is not None:
                parser.error("--plan is only used with --apply or --audit")
            report = survey(args.config, args.symbol)
            if args.plan_out is not None:
                if args.plan_out.is_symlink() or args.plan_out.exists():
                    raise ValueError("refusing to overwrite existing source plan")
                atomic_json(args.plan_out, report)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not args.audit or report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
