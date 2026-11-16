"""Immutable local record of *attempted* public evidence polls.

Unlike a price receipt, an attempt record exists even if TLS fails or the
collector crashes after writing intent. Only observed attempts are counted;
nothing is inferred about downtime before/after an actual collection run.
These local hashes are NOT an independent source or clock attestation.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_acquisition import _immutably_save
from lab.bearish_hybrid_forward_price import load_forward_prices
from lab.bearish_hybrid_public_capture import _validate_symbol, load_public_features
from lab.persistence import parse_json

MAX_ATTEMPTS = 100_000


@dataclass(frozen=True)
class AttemptStart:
    symbol: str
    scheduled_ns: int
    started_ns: int
    index: int
    oi_due: bool
    funding_due: bool


@dataclass(frozen=True)
class AttemptFinish:
    finished_ns: int
    price_received_ns: int | None
    price_bar_end_ns: int | None
    oi_received_ns: int | None
    funding_received_ns: int | None
    network_failures: tuple[str, ...]
    aborted_error: str | None


@dataclass(frozen=True)
class Attempt:
    start: AttemptStart
    finish: AttemptFinish | None


@dataclass(frozen=True)
class Coverage:
    attempts: int
    completed: int
    interrupted: int
    aborted: int
    price_received: int
    price_missing: int
    oi_due: int
    oi_received: int
    funding_due: int
    funding_received: int
    late_attempts: int
    wrong_minute_price: int
    independent_source_authenticated: bool = False
    qualified: bool = False
    actual_notional: int = 0


def _folder(root: Path, symbol: str) -> Path:
    _validate_symbol(symbol)
    return Path(root) / "binance-usdm" / "forward-attempts" / symbol


def _prefix(start: AttemptStart) -> str:
    return f"{start.started_ns}-{start.index:03d}"


def record_attempt_start(
    root: Path, symbol: str, scheduled_ns: int, started_ns: int, index: int
) -> AttemptStart:
    """Persist collection intent BEFORE the first public network request."""
    _validate_symbol(symbol)
    if (
        type(scheduled_ns) is not int
        or scheduled_ns <= 0
        or scheduled_ns % MINUTE_NS
        or type(started_ns) is not int
        or started_ns < scheduled_ns
        or type(index) is not int
        or index < 0
        or index >= 120
    ):
        raise ValueError("invalid attempted UTC minute")
    start = AttemptStart(symbol, scheduled_ns, started_ns, index, index % 6 == 0, index % 60 == 0)
    payload = {
        "version": 1,
        "symbol": symbol,
        "scheduled_ns": scheduled_ns,
        "started_ns": started_ns,
        "index": index,
        "oi_due": start.oi_due,
        "funding_due": start.funding_due,
        "independently_authenticated": False,
        "qualified": False,
        "actual_notional": 0,
    }
    _immutably_save(
        _folder(root, symbol) / f"{_prefix(start)}.start.json",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return start


def record_attempt_finish(
    root: Path,
    start: AttemptStart,
    *,
    finished_ns: int | None = None,
    price_received_ns: int | None = None,
    price_bar_end_ns: int | None = None,
    oi_received_ns: int | None = None,
    funding_received_ns: int | None = None,
    network_failures: tuple[str, ...] = (),
    aborted_error: str | None = None,
) -> AttemptFinish:
    """Persist terminal attempt state without overwriting the intent record.

    An error is class-only; no exception content, URLs with secrets or private
    account context is retained. References are checked offline on reload.
    """
    if not isinstance(start, AttemptStart):
        raise ValueError("missing recorded start")
    end = time.time_ns() if finished_ns is None else finished_ns
    if type(end) is not int or end < start.started_ns:
        raise ValueError("invalid attempt end clock")
    refs = (price_received_ns, price_bar_end_ns, oi_received_ns, funding_received_ns)
    if any(v is not None and (type(v) is not int or v <= 0 or v > end) for v in refs):
        raise ValueError("invalid attempt receipt reference")
    if (price_received_ns is None) != (price_bar_end_ns is None):
        raise ValueError("incomplete price receipt reference")
    if oi_received_ns is not None and not start.oi_due:
        raise ValueError("OI receipt on non-OI poll")
    if funding_received_ns is not None and not start.funding_due:
        raise ValueError("funding receipt on non-funding poll")
    if (
        type(network_failures) is not tuple
        or len(network_failures) > 3
        or any(
            type(e) is not str
            or re.fullmatch(r"(price|open-interest|settled-funding):[A-Za-z]{1,64}", e) is None
            for e in network_failures
        )
        or len(network_failures) != len(set(network_failures))
        or (aborted_error is not None and (type(aborted_error) is not str or re.fullmatch(r"[A-Za-z]{1,64}", aborted_error) is None))
    ):
        raise ValueError("invalid attempt failure metadata")
    finish = AttemptFinish(
        end, price_received_ns, price_bar_end_ns, oi_received_ns, funding_received_ns, network_failures, aborted_error
    )
    payload = {
        "version": 1,
        "symbol": start.symbol,
        "started_ns": start.started_ns,
        "index": start.index,
        "finished_ns": end,
        "price_received_ns": price_received_ns,
        "price_bar_end_ns": price_bar_end_ns,
        "oi_received_ns": oi_received_ns,
        "funding_received_ns": funding_received_ns,
        "network_failures": list(network_failures),
        "aborted_error": aborted_error,
        "independently_authenticated": False,
        "qualified": False,
        "actual_notional": 0,
    }
    intent = _folder(root, start.symbol) / f"{_prefix(start)}.start.json"
    if not intent.is_file() or intent.is_symlink():
        raise ValueError("missing immutable attempt start")
    outcome = _folder(root, start.symbol) / f"{_prefix(start)}.finish.json"
    if outcome.exists() or outcome.is_symlink():
        raise ValueError("attempt already finalized")
    _immutably_save(outcome, (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return finish


def _load_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("untrusted attempt receipt file")
    try:
        obj = parse_json(path.read_bytes())
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid attempt receipt") from exc
    if type(obj) is not dict:
        raise ValueError("invalid attempt receipt object")
    return obj


def load_attempts(root: Path, symbol: str) -> tuple[Attempt, ...]:
    """Offline-only exhaustive receipt verification, including orphan starts."""
    folder = _folder(root, symbol)
    if folder.is_symlink() or any(parent.is_symlink() for parent in folder.parents):
        raise ValueError("untrusted attempt directory")
    if not folder.exists():
        return ()
    paths = list(folder.iterdir())
    if len(paths) > 2 * MAX_ATTEMPTS:
        raise ValueError("too many attempt files")
    entries: dict[str, dict[str, Path]] = {}
    for path in paths:
        m = re.fullmatch(r"([0-9]+-[0-9]{3})\.(start|finish)\.json", path.name)
        if m is None or path.is_symlink() or not path.is_file():
            raise ValueError("unknown or unsafe attempt record")
        entries.setdefault(m[1], {})[m[2]] = path

    # These loaders independently verify the actual raw source response bytes
    # and receipt-clocks; a forged attempt result alone cannot prove a capture.
    prices = {r.received_ns: r for r in load_forward_prices(root, symbol)}
    oi = {r.received_ns for r in load_public_features(root, symbol, "open-interest")}
    funding = {r.received_ns for r in load_public_features(root, symbol, "settled-funding")}

    result: list[Attempt] = []
    seen_starts: set[int] = set()
    for key in sorted(entries, key=lambda x: (int(x.split("-")[0]), int(x.split("-")[1]))):
        files = entries[key]
        if "start" not in files:
            raise ValueError("attempt outcome without original intent")
        a = _load_object(files["start"])
        if set(a) != {
            "version", "symbol", "scheduled_ns", "started_ns", "index",
            "oi_due", "funding_due", "independently_authenticated", "qualified", "actual_notional",
        }:
            raise ValueError("unrecognized attempt start schema")
        ns, idx, scheduled = a["started_ns"], a["index"], a["scheduled_ns"]
        if (
            type(ns) is not int
            or ns < scheduled
            or ns in seen_starts
            or type(idx) is not int
            or not 0 <= idx < 120
            or key != f"{ns}-{idx:03d}"
            or type(scheduled) is not int
            or scheduled <= 0
            or scheduled % MINUTE_NS
            or a["version"] != 1
            or type(a["version"]) is not int
            or a["symbol"] != symbol
            or a["oi_due"] is not (idx % 6 == 0)
            or a["funding_due"] is not (idx % 60 == 0)
            or a["independently_authenticated"] is not False
            or a["qualified"] is not False
            or type(a["actual_notional"]) is not int
            or a["actual_notional"] != 0
        ):
            raise ValueError("untrusted attempt start")
        seen_starts.add(ns)
        start = AttemptStart(symbol, scheduled, ns, idx, a["oi_due"], a["funding_due"])
        finish = None
        if "finish" in files:
            b = _load_object(files["finish"])
            if set(b) != {
                "version", "symbol", "started_ns", "index", "finished_ns",
                "price_received_ns", "price_bar_end_ns", "oi_received_ns",
                "funding_received_ns", "network_failures", "aborted_error",
                "independently_authenticated", "qualified", "actual_notional",
            }:
                raise ValueError("unrecognized attempt finish schema")
            if (
                b["version"] != 1
                or type(b["version"]) is not int
                or b["symbol"] != symbol
                or type(b["started_ns"]) is not int
                or b["started_ns"] != ns
                or type(b["index"]) is not int
                or b["index"] != idx
                or b["independently_authenticated"] is not False
                or b["qualified"] is not False
                or type(b["actual_notional"]) is not int
                or b["actual_notional"] != 0
                or type(b["network_failures"]) is not list
            ):
                raise ValueError("untrusted attempt finish")
            # Do not rewrite source: validate through the same constructor
            # checks as the recorder, but without writing to disk.
            end = b["finished_ns"]
            receipts = (
                b["price_received_ns"], b["price_bar_end_ns"],
                b["oi_received_ns"], b["funding_received_ns"],
            )
            if type(end) is not int or end < ns or any(
                v is not None and (type(v) is not int or not ns <= v <= end) for v in receipts
            ):
                raise ValueError("invalid attempt result clocks")
            if (receipts[0] is None) != (receipts[1] is None):
                raise ValueError("partial price receipt")
            if receipts[0] is not None and (
                receipts[0] not in prices or prices[receipts[0]].bar.end_ns != receipts[1]
            ):
                raise ValueError("attempt refers to missing/mismatched saved price")
            if receipts[2] is not None and (not start.oi_due or receipts[2] not in oi):
                raise ValueError("attempt refers to missing/unexpected OI")
            if receipts[3] is not None and (not start.funding_due or receipts[3] not in funding):
                raise ValueError("attempt refers to missing/unexpected funding")
            failures = b["network_failures"]
            if len(failures) > 3 or any(
                type(e) is not str
                or re.fullmatch(r"(price|open-interest|settled-funding):[A-Za-z]{1,64}", e) is None
                for e in failures
            ) or len(failures) != len(set(failures)):
                raise ValueError("invalid saved network failures")
            aborted = b["aborted_error"]
            if aborted is not None and (type(aborted) is not str or re.fullmatch(r"[A-Za-z]{1,64}", aborted) is None):
                raise ValueError("invalid saved aborted error")
            finish = AttemptFinish(end, receipts[0], receipts[1], receipts[2], receipts[3], tuple(failures), aborted)
        result.append(Attempt(start, finish))
    return tuple(result)


def coverage_from_attempts(attempts: tuple[Attempt, ...]) -> Coverage:
    """Coverage among *recorded attempts*, not a claim about 24/7 uptime."""
    if any(not isinstance(a, Attempt) for a in attempts):
        raise ValueError("expected validated attempt records")
    finished = [a for a in attempts if a.finish is not None]
    return Coverage(
        attempts=len(attempts),
        completed=sum(a.finish.aborted_error is None for a in finished),
        interrupted=len(attempts) - len(finished),
        aborted=sum(a.finish.aborted_error is not None for a in finished),
        price_received=sum(a.finish.price_received_ns is not None for a in finished),
        price_missing=sum(a.finish is None or a.finish.price_received_ns is None for a in attempts),
        oi_due=sum(a.start.oi_due for a in attempts),
        oi_received=sum(a.finish.oi_received_ns is not None for a in finished),
        funding_due=sum(a.start.funding_due for a in attempts),
        funding_received=sum(a.finish.funding_received_ns is not None for a in finished),
        late_attempts=sum(a.start.started_ns - a.start.scheduled_ns > 10_000_000_000 for a in attempts),
        wrong_minute_price=sum(
            a.finish.price_bar_end_ns is not None and a.finish.price_bar_end_ns != a.start.scheduled_ns
            for a in finished
        ),
    )
