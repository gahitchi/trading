"""Source-pinned, holdout-isolated Binance USD-M funding settlement evidence.

Survey/plan: download ONLY explicitly bounded pre-holdout settlement history
and pin every exact API page by SHA-256. Apply: requery and compare the
entire source manifest *before* storing an append-only evidence cache.
Audit: verify the cache locally without contacting the API.

Settlement accounting below is a pure function. It is NOT connected to
crash-study P&L until historical funding evidence is independently accepted.
No funding rate, mark price, cadence, or fill is ever imputed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from lab.config import load_config
from lab.crash_oi_migration import _holdout_boundary
from lab.market_symbols import SYMBOL_RE
from lab.persistence import atomic_json, read_json

ENDPOINT = "https://fapi.binance.com/fapi/v1/fundingRate"
KIND = "binance-usdm-funding-history-preholdout-v2"
LIMIT = 1000
MAX_PAGES = 100
# Preserve actual exchange milliseconds. Funding history can be slightly
# displaced from its nominal hour; fail closed on larger offsets.
MAX_SETTLEMENT_OFFSET_MS = 30_000
MAX_GAP = pd.Timedelta(hours=8, seconds=30)
UTC = "UTC"


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _decimal(raw, *, positive: bool = False) -> str:
    if not isinstance(raw, str):
        raise ValueError("funding rate and mark price must be exchange decimal strings")
    try:
        d = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("invalid funding decimal string") from exc
    if not d.is_finite() or (positive and d <= 0) or abs(d) > Decimal("1000000000"):
        raise ValueError("nonfinite or invalid funding rate/mark price")
    return str(d)


def _event(row: dict, symbol: str, first_ms: int, end_ms: int) -> dict:
    if not isinstance(row, dict) or row.get("symbol") != symbol:
        raise ValueError("funding event symbol mismatch")
    ts = row.get("fundingTime")
    if type(ts) is not int:
        raise ValueError("funding event timestamp must be integer milliseconds")
    if not (first_ms <= ts <= end_ms):
        raise ValueError(
            f"funding event outside protected research time: "
            f"{pd.Timestamp(ts, unit='ms', tz=UTC).isoformat()} "
            f"(permitted milliseconds {first_ms}..{end_ms})"
        )
    # Historical fundingTime is an exact exchange millisecond timestamp,
    # NOT guaranteed to be an exact UTC minute. Keep the original value
    # rather than rounding and silently changing settlement ownership.
    # BTC USD-M settlements should nevertheless be near a clock hour.
    offset = ts % 3_600_000
    if min(offset, 3_600_000 - offset) > MAX_SETTLEMENT_OFFSET_MS:
        raise ValueError(
            f"funding event unexpectedly far from UTC funding hour: "
            f"{pd.Timestamp(ts, unit='ms', tz=UTC).isoformat()}"
        )
    if row.get("rateType", "Regular") != "Regular":
        raise ValueError("unsupported funding rate type")
    rate = _decimal(row.get("fundingRate"))
    mark = _decimal(row.get("markPrice"), positive=True)
    if abs(Decimal(rate)) >= 1:
        raise ValueError("implausible funding rate magnitude")
    return {"fundingTime": ts, "fundingRate": rate, "markPrice": mark}


def _get(url: str):
    with urlopen(Request(url, headers={"User-Agent": "trading-lab-funding-audit"}), timeout=60) as response:
        raw = response.read()
    return json.loads(raw)


def _fetch(symbol: str, start: date, boundary: date, get) -> dict:
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid perpetual symbol")
    start_time = pd.Timestamp(start, tz=UTC)
    stop_time = pd.Timestamp(boundary, tz=UTC)
    if not start_time < stop_time:
        raise ValueError("funding start must precede protected holdout")
    first_ms = int(start_time.value // 1_000_000)
    end_ms = int(stop_time.value // 1_000_000) - 1
    cursor = first_ms
    events: list[dict] = []
    pages = []
    previous_ms = None
    for _ in range(MAX_PAGES):
        params = urlencode({
            "symbol": symbol,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": LIMIT,
        })
        url = f"{ENDPOINT}?{params}"
        raw = get(url)
        if not isinstance(raw, list) or len(raw) > LIMIT:
            raise ValueError("invalid funding API page")
        normalized = [_event(row, symbol, cursor, end_ms) for row in raw]
        times = [item["fundingTime"] for item in normalized]
        if any(a >= b for a, b in zip(times, times[1:])) or (
            previous_ms is not None and times and times[0] <= previous_ms
        ):
            raise ValueError("duplicate or unordered funding source timestamps")
        pages.append({
            "start_ms": cursor,
            "end_ms": end_ms,
            "rows": len(normalized),
            "source_sha256": _digest(raw),
        })
        events.extend(normalized)
        if not normalized or len(normalized) < LIMIT:
            break
        previous_ms = times[-1]
        cursor = previous_ms + 1
        if cursor > end_ms:
            break
    else:
        raise ValueError("funding pagination exceeds safety bound")

    # Bounds/continuity are a fail-closed heuristic, not a claim that
    # exchange funding always settles every 8 h for every symbol.
    if not events:
        raise ValueError("no historical funding settlements returned")
    clock = pd.to_datetime([x["fundingTime"] for x in events], unit="ms", utc=True)
    if clock[0] - start_time > MAX_GAP or stop_time - clock[-1] > MAX_GAP:
        raise ValueError("funding endpoint coverage gap exceeds eight hours")
    if len(clock) > 1 and (clock[1:] - clock[:-1] > MAX_GAP).any():
        raise ValueError("historical funding settlement gap exceeds eight hours")

    source = {
        "version": 2, "kind": KIND, "symbol": symbol,
        "start": start.isoformat(), "holdout_start": boundary.isoformat(),
        "pages": pages, "event_count": len(events),
        "events_sha256": _digest(events),
    }
    return {"plan": source, "events": events}


def _validate_plan(plan: dict, symbol: str, boundary: date) -> None:
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid funding perpetual symbol")
    if not isinstance(plan, dict) or set(plan) != {
        "version", "kind", "symbol", "start", "holdout_start",
        "pages", "event_count", "events_sha256",
    }:
        raise ValueError("invalid source plan")
    if plan["version"] != 2 or plan["kind"] != KIND or plan["symbol"] != symbol:
        raise ValueError("funding plan identity mismatch")
    if plan["holdout_start"] != boundary.isoformat():
        raise ValueError("funding plan does not match protected holdout")
    day = date.fromisoformat(plan["start"])
    if not day < boundary or not isinstance(plan["pages"], list) or not plan["pages"]:
        raise ValueError("invalid funding plan coverage")
    if type(plan["event_count"]) is not int or plan["event_count"] <= 0:
        raise ValueError("invalid funding plan event count")
    if any(not isinstance(page, dict) for page in plan["pages"]):
        raise ValueError("invalid funding plan page")
    if sum(page.get("rows", 0) for page in plan["pages"]) != plan["event_count"]:
        raise ValueError("funding page row count differs from source event count")
    for entry in plan["pages"]:
        if set(entry) != {
            "start_ms", "end_ms", "rows", "source_sha256",
        }:
            raise ValueError("invalid funding source page manifest")
        if any(type(entry[x]) is not int for x in ("start_ms", "end_ms", "rows")):
            raise ValueError("invalid funding page bounds")
        if not 0 <= entry["rows"] <= LIMIT or entry["start_ms"] > entry["end_ms"]:
            raise ValueError("invalid funding page size")
        digest = entry["source_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid funding page SHA-256")
    for earlier, later in zip(plan["pages"], plan["pages"][1:]):
        if earlier["end_ms"] != later["end_ms"] or earlier["start_ms"] >= later["start_ms"]:
            raise ValueError("funding pagination source cursor not strictly advancing")
    digest = plan["events_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid funding events SHA-256")


def _directory(config_path: Path, symbol: str) -> Path:
    cfg = load_config(config_path)
    base = cfg.data_dir / "crash" / "funding-pinned"
    directory = base / symbol
    for part in (cfg.data_dir, cfg.data_dir / "crash", base, directory):
        if part.is_symlink():
            raise ValueError(f"unsafe funding cache directory: {part}")
    return directory


def _path(directory: Path, plan: dict) -> Path:
    return directory / f"settlements-{plan['start']}-to-{plan['holdout_start']}.json"


def survey(config_path: Path, *, symbol: str = "BTCUSDT", start: date = date(2024, 1, 1), get=None) -> dict:
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    _directory(config_path, symbol)
    return _fetch(symbol, start, boundary, get or _get)["plan"]


def apply(config_path: Path, plan_path: Path, *, symbol: str = "BTCUSDT", get=None) -> dict:
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    plan = read_json(plan_path)
    _validate_plan(plan, symbol, boundary)
    fetched = _fetch(symbol, date.fromisoformat(plan["start"]), boundary, get or _get)
    if fetched["plan"] != plan:
        raise ValueError("funding source page or event SHA-256 changed after preflight")
    directory = _directory(config_path, symbol)
    target = _path(directory, plan)
    data = {"plan": plan, "events": fetched["events"], "plan_sha256": _digest(plan)}
    if directory.exists() and target.is_symlink():
        raise ValueError("unsafe funding cache symlink")
    if target.exists():
        if read_json(target) != data:
            raise ValueError("existing funding evidence differs from source-pinned plan")
        return {"status": "verified-existing", "events": len(fetched["events"]), "read_only": False}
    directory.mkdir(parents=True, exist_ok=True)
    # No-clobber publish: stage/fsync a temporary file, then hard-link it
    # into place. os.link fails atomically if another writer created target.
    # Never invoke os.replace on an already-present funding evidence file.
    raw = json.dumps(data, sort_keys=True, allow_nan=False, indent=2)
    fd, name = tempfile.mkstemp(prefix=target.name + ".partial-", dir=directory)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.link(tmp, target)
        from lab.crash_oi_migration import _sync_directory
        _sync_directory(directory)
    finally:
        tmp.unlink(missing_ok=True)
    return {"status": "committed", "events": len(fetched["events"]), "read_only": False}


def audit(config_path: Path, plan_path: Path, *, symbol: str = "BTCUSDT") -> dict:
    cfg = load_config(config_path)
    boundary = _holdout_boundary(cfg)
    plan = read_json(plan_path)
    _validate_plan(plan, symbol, boundary)
    target = _path(_directory(config_path, symbol), plan)
    issues = []
    try:
        if target.is_symlink():
            raise ValueError("unsafe funding cache symlink")
        cache = read_json(target)
        if set(cache) != {"plan", "events", "plan_sha256"} or cache["plan"] != plan:
            raise ValueError("funding cache plan does not match pinned source")
        if cache["plan_sha256"] != _digest(plan) or _digest(cache["events"]) != plan["events_sha256"]:
            raise ValueError("funding cache digest mismatch")
        first = int(pd.Timestamp(plan["start"], tz=UTC).value // 1_000_000)
        last = int(pd.Timestamp(boundary, tz=UTC).value // 1_000_000) - 1
        entries = [_event(v | {"symbol": symbol}, symbol, first, last) for v in cache["events"]]
        if entries != cache["events"] or len(entries) != plan["event_count"]:
            raise ValueError("invalid cached funding event content")
        ticks = [v["fundingTime"] for v in entries]
        if ticks != sorted(set(ticks)):
            raise ValueError("unsorted or duplicate cached funding events")
        _check_coverage(ticks, plan["start"], plan["holdout_start"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        issues.append(f"{type(exc).__name__}: {exc}")
    return {"ok": not issues, "read_only": True, "issues": issues, "events": plan["event_count"]}


def _check_coverage(times_ms: list[int], start: str, stop: str) -> None:
    if not times_ms:
        raise ValueError("empty funding event timeline")
    first = pd.Timestamp(start, tz=UTC)
    end = pd.Timestamp(stop, tz=UTC)
    timeline = pd.to_datetime(times_ms, unit="ms", utc=True)
    if timeline[0] - first > MAX_GAP or end - timeline[-1] > MAX_GAP:
        raise ValueError("funding edge coverage exceeds eight hours")
    if len(timeline) > 1 and (timeline[1:] - timeline[:-1] > MAX_GAP).any():
        raise ValueError("funding internal coverage gap exceeds eight hours")


def settlement_return(
    side: int, entry_reference: float, entry_time: pd.Timestamp,
    exit_time: pd.Timestamp, events: list[dict],
) -> dict:
    """USDT-M fixed-base funding return relative to entry-reference notional.

    Positive funding rate pays shorts and charges longs. Only strictly
    interior settlements can be assigned to a position with minute-
    resolution trade timestamps. An event at entry or exit is ambiguous
    and MUST fail closed rather than assuming profit or zero cost.
    """
    if type(side) is not int or side not in (-1, 1):
        raise ValueError("invalid perpetual position side")
    if not isinstance(entry_reference, (float, int)) or not math.isfinite(entry_reference) or entry_reference <= 0:
        raise ValueError("invalid position entry reference price")
    entry = pd.Timestamp(entry_time)
    exit_ = pd.Timestamp(exit_time)
    if entry.tzinfo is None or exit_.tzinfo is None or entry >= exit_:
        raise ValueError("funding position interval must be ordered and timezone-aware")
    if entry.utcoffset() != pd.Timedelta(0) or exit_.utcoffset() != pd.Timedelta(0):
        raise ValueError("funding position interval must use UTC timestamps")
    if not events:
        raise ValueError("no funding settlement evidence; cannot assume zero funding")
    paid = Decimal("0")
    count = 0
    previous = None
    for event in events:
        event_time = pd.Timestamp(event["fundingTime"], unit="ms", tz=UTC)
        if previous is not None and event_time <= previous:
            raise ValueError("funding events must be unique and chronologically ordered")
        previous = event_time
        if (
            abs(event_time - entry) <= pd.Timedelta(milliseconds=MAX_SETTLEMENT_OFFSET_MS)
            or abs(event_time - exit_) <= pd.Timedelta(milliseconds=MAX_SETTLEMENT_OFFSET_MS)
        ):
            raise ValueError("funding settlement coincides with ambiguous entry/exit boundary")
        if entry < event_time < exit_:
            rate = Decimal(_decimal(event["fundingRate"]))
            mark = Decimal(_decimal(event["markPrice"], positive=True))
            paid += -side * rate * mark
            count += 1
    return {
        "settlements": count,
        "funding_return": float(paid / Decimal(str(entry_reference))),
        "notional_convention": "fixed-base-settlement-mark-v1",
        "coverage_asserted": False,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Source-pinned pre-holdout USD-M funding settlement evidence")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", default="2024-01-01")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan-out", type=Path)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--audit", action="store_true")
    p.add_argument("--plan", type=Path)
    args = p.parse_args(argv)
    try:
        if args.apply or args.audit:
            if args.plan is None:
                p.error("--apply/--audit require a source-pinned --plan")
            result = (
                apply(args.config, args.plan, symbol=args.symbol)
                if args.apply else audit(args.config, args.plan, symbol=args.symbol)
            )
        else:
            if args.plan is not None:
                p.error("--plan requires --apply or --audit")
            result = survey(args.config, symbol=args.symbol, start=date.fromisoformat(args.start))
            if args.plan_out is not None:
                if args.plan_out.exists() or args.plan_out.is_symlink():
                    raise ValueError("refusing to overwrite existing funding source plan")
                atomic_json(args.plan_out, result)
    except (ValueError, OSError, RuntimeError, TypeError, KeyError) as exc:
        p.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not args.audit or result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
