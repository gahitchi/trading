"""Durable Binance USD-M public mark and historical funding observations.

Research/paper only. No account credentials or orders. A public mark is
not an authenticated account fill, an executed funding payment, or P&L.
The API's latest funding rate is metadata; the history endpoint supplies
a past *rate*, NOT an account-specific cash settlement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import Request, urlopen

from lab.persistence import atomic_json, file_lock, parse_json, read_json

_ORIGIN = "binance-usdm-public-rest"
_BASE = "https://fapi.binance.com/fapi/v1"
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SYMBOL = re.compile(r"[A-Z0-9]{2,16}USDT\Z")
_VERSION = 1
_NS_MS = 1_000_000
_MAX_BODY = 65536
_MAX_QUERY_NS = 10_000_000_000
_MAX_MARK_LAG_MS = 30_000
_MAX_CLOCK_LEAD_MS = 2_000
_MAX_HISTORY_AGE_MS = 7 * 86_400_000


@dataclass(frozen=True)
class PublicMarketSample:
    session_id: str
    instrument_id: str
    symbol: str
    mark_start_ns: int
    mark_end_ns: int
    funding_start_ns: int
    funding_end_ns: int
    venue_mark_time_ms: int
    mark_price: str
    index_price: str
    latest_funding_rate: str
    next_funding_time_ms: int
    historical_funding_time_ms: int
    historical_funding_rate: str
    historical_funding_mark_price: str
    mark_sha256: str
    funding_sha256: str
    mark_raw_json: str
    funding_raw_json: str


def _exact(value: object, label: str, *, positive: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an exact decimal string")
    try:
        x = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {label}") from exc
    if not x.is_finite() or (positive and x <= 0):
        raise ValueError(f"invalid {label}")
    if not positive and abs(x) > 1:
        raise ValueError(f"unbounded {label}")
    return value


def _stamp(value, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {name} timestamp")
    return value


def _json(raw: bytes, kind: str):
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_BODY:
        raise ValueError(f"unbounded or missing {kind} API response")
    try:
        return parse_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {kind} API JSON") from exc


def _identifiers(session_id: str, symbol: str, instrument_id: str):
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise ValueError("invalid public market session")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("invalid USD-M USDT symbol")
    if instrument_id != f"{symbol}-PERP.BINANCE":
        raise ValueError("public mark instrument does not match USD-M symbol")


def parse_public_market_sample(
    *,
    session_id: str,
    symbol: str,
    mark_raw: bytes,
    funding_raw: bytes,
    mark_start_ns: int,
    mark_end_ns: int,
    funding_start_ns: int,
    funding_end_ns: int,
) -> PublicMarketSample:
    """Parse two exact REST payloads and validate their observation times.

    No capture clock is silently substituted for the venue's mark time.
    Each response is preserved byte-for-byte through UTF-8 roundtrips.
    """
    instrument_id = f"{symbol}-PERP.BINANCE"
    _identifiers(session_id, symbol, instrument_id)
    for name, before, after in (
        ("mark", mark_start_ns, mark_end_ns),
        ("funding", funding_start_ns, funding_end_ns),
    ):
        _stamp(before, f"{name} request start")
        _stamp(after, f"{name} request end")
        if not before <= after <= before + _MAX_QUERY_NS:
            raise ValueError(f"{name} observation took too long or reversed time")
    if funding_start_ns < mark_end_ns:
        raise ValueError("funding observation predates completion of mark request")

    mark = _json(mark_raw, "mark")
    history = _json(funding_raw, "funding")
    if not isinstance(mark, dict) or not isinstance(history, list) or len(history) != 1:
        raise ValueError("unexpected public mark or historical funding response shape")
    item = history[0]
    if not isinstance(item, dict):
        raise ValueError("unexpected historical funding row")
    if mark.get("symbol") != symbol or item.get("symbol") != symbol:
        raise ValueError("public market evidence symbol mismatch")

    price = _exact(mark.get("markPrice"), "mark price", positive=True)
    index = _exact(mark.get("indexPrice"), "index price", positive=True)
    latest = _exact(mark.get("lastFundingRate"), "latest funding rate")
    rate = _exact(item.get("fundingRate"), "historical funding rate")
    funded_price = _exact(item.get("markPrice"), "historical funding mark", positive=True)
    venue_time = _stamp(mark.get("time"), "mark venue")
    next_funding = _stamp(mark.get("nextFundingTime"), "next funding")
    history_time = _stamp(item.get("fundingTime"), "historical funding")
    if venue_time < mark_start_ns // _NS_MS - _MAX_MARK_LAG_MS:
        raise ValueError("public mark evidence is stale")
    if venue_time > mark_end_ns // _NS_MS + _MAX_CLOCK_LEAD_MS:
        raise ValueError("public mark venue clock is ahead of collection")
    if next_funding <= venue_time:
        raise ValueError("public mark has an expired next-funding time")
    if history_time > funding_end_ns // _NS_MS + _MAX_CLOCK_LEAD_MS:
        raise ValueError("funding history includes an unobserved future event")
    if history_time < funding_end_ns // _NS_MS - _MAX_HISTORY_AGE_MS:
        raise ValueError("historical funding rate is too old for current snapshot")
    return PublicMarketSample(
        session_id=session_id,
        instrument_id=instrument_id,
        symbol=symbol,
        mark_start_ns=mark_start_ns,
        mark_end_ns=mark_end_ns,
        funding_start_ns=funding_start_ns,
        funding_end_ns=funding_end_ns,
        venue_mark_time_ms=venue_time,
        mark_price=price,
        index_price=index,
        latest_funding_rate=latest,
        next_funding_time_ms=next_funding,
        historical_funding_time_ms=history_time,
        historical_funding_rate=rate,
        historical_funding_mark_price=funded_price,
        mark_sha256=hashlib.sha256(mark_raw).hexdigest(),
        funding_sha256=hashlib.sha256(funding_raw).hexdigest(),
        mark_raw_json=mark_raw.decode("utf-8"),
        funding_raw_json=funding_raw.decode("utf-8"),
    )


def _get_raw(url: str) -> bytes:
    # A fixed Binance HTTPS host, never a caller-supplied URL.
    with urlopen(Request(url, headers={"User-Agent": "trading-lab-public-evidence/1"}), timeout=8) as response:
        if response.status != 200:
            raise ValueError("public mark/funding endpoint returned non-200")
        raw = response.read(_MAX_BODY + 1)
    if not 0 < len(raw) <= _MAX_BODY:
        raise ValueError("public mark/funding endpoint returned an unbounded response")
    return raw


def fetch_public_market_sample(
    session_id: str,
    symbol: str,
    *,
    get=_get_raw,
    clock=time.time_ns,
) -> PublicMarketSample:
    """Two fixed, public, read-only GETs. Injected I/O for offline tests."""
    _identifiers(session_id, symbol, f"{symbol}-PERP.BINANCE")
    mark_start = clock()
    mark_raw = get(f"{_BASE}/premiumIndex?symbol={symbol}")
    mark_end = clock()
    funding_start = clock()
    funding_raw = get(f"{_BASE}/fundingRate?symbol={symbol}&limit=1")
    funding_end = clock()
    return parse_public_market_sample(
        session_id=session_id,
        symbol=symbol,
        mark_raw=mark_raw,
        funding_raw=funding_raw,
        mark_start_ns=mark_start,
        mark_end_ns=mark_end,
        funding_start_ns=funding_start,
        funding_end_ns=funding_end,
    )


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _seal(value: dict) -> dict:
    return {**value, "sha256": hashlib.sha256(_canonical(value)).hexdigest()}


def _check_record(value: object, schema: set[str]):
    if not isinstance(value, dict) or set(value) != schema | {"sha256"}:
        raise ValueError("public market evidence archive schema mismatch")
    digest = value["sha256"]
    if not isinstance(digest, str) or _seal({k: v for k, v in value.items() if k != "sha256"})["sha256"] != digest:
        raise ValueError("public market evidence archive hash mismatch")


def _sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(fd: int, raw: bytes):
    rest = memoryview(raw)
    while rest:
        n = os.write(fd, rest)
        if n <= 0:
            raise OSError("public evidence archive append made no progress")
        rest = rest[n:]


def _revalidate(sample: PublicMarketSample):
    value = parse_public_market_sample(
        session_id=sample.session_id,
        symbol=sample.symbol,
        mark_raw=sample.mark_raw_json.encode("utf-8"),
        funding_raw=sample.funding_raw_json.encode("utf-8"),
        mark_start_ns=sample.mark_start_ns,
        mark_end_ns=sample.mark_end_ns,
        funding_start_ns=sample.funding_start_ns,
        funding_end_ns=sample.funding_end_ns,
    )
    if sample != value:
        raise ValueError("public market evidence fields differ from raw source payloads")


class PublicMarketEvidenceJournal:
    """An append-only audit aid, never a source of realized funding payments."""

    def __init__(self, path: Path, *, session_id: str, symbol: str):
        _identifiers(session_id, symbol, f"{symbol}-PERP.BINANCE")
        self.path = Path(path)
        self.session_id = session_id
        self.symbol = symbol
        self._head = self.path.with_name(self.path.name + ".head.json")
        self._lock = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_paths()
        with file_lock(self._lock):
            if not self.path.exists():
                if self._head.exists():
                    raise ValueError("public evidence archive absent but checkpoint exists")
                header = _seal(self._header())
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                try:
                    _write(fd, _canonical(header) + b"\n")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _sync_dir(self.path.parent)
                self._head_write(0, header["sha256"], header["sha256"])
            self._read_locked()

    def _header(self):
        return {
            "version": _VERSION,
            "type": "public-market-evidence-header",
            "origin": _ORIGIN,
            "session_id": self.session_id,
            "instrument_id": f"{self.symbol}-PERP.BINANCE",
        }

    def _check_paths(self):
        if any(p.is_symlink() for p in (self.path, self._head, self._lock)):
            raise ValueError("public evidence archive paths must not be symbolic links")

    def _head_write(self, count: int, previous: str, header: str):
        atomic_json(
            self._head, _seal({"version": _VERSION, "count": count, "last_sha256": previous, "header_sha256": header})
        )

    def _read_locked(self) -> tuple[tuple[PublicMarketSample, ...], str, str]:
        self._check_paths()
        raw = self.path.read_bytes()
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("public market evidence archive has incomplete suffix")
        try:
            rows = [parse_json(line.decode("utf-8")) for line in raw.splitlines()]
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ValueError("public market archive has invalid JSON") from exc
        header = rows[0]
        _check_record(header, set(self._header()))
        if {k: v for k, v in header.items() if k != "sha256"} != self._header():
            raise ValueError("public market evidence session or source mismatch")
        samples = []
        last = header["sha256"]
        expected = {f.name for f in fields(PublicMarketSample)}
        for sequence, row in enumerate(rows[1:], 1):
            _check_record(row, {"type", "version", "sequence", "previous_sha256", "sample"})
            if (
                row["type"] != "public-market-sample"
                or row["version"] != _VERSION
                or type(row["sequence"]) is not int
                or row["sequence"] != sequence
                or row["previous_sha256"] != last
            ):
                raise ValueError("public market archive sequence mismatch")
            sample_data = row["sample"]
            if not isinstance(sample_data, dict) or set(sample_data) != expected:
                raise ValueError("public market sample schema mismatch")
            sample = PublicMarketSample(**sample_data)
            _revalidate(sample)
            if sample.session_id != self.session_id or sample.symbol != self.symbol:
                raise ValueError("public market sample scope mismatch")
            if samples and sample.mark_start_ns <= samples[-1].funding_end_ns:
                raise ValueError("public market sample requests overlap or are not chronological")
            samples.append(sample)
            last = row["sha256"]
        if not self._head.exists():
            raise ValueError("public market archive checkpoint missing")
        checkpoint = read_json(self._head)
        _check_record(checkpoint, {"version", "count", "last_sha256", "header_sha256"})
        if (
            checkpoint["version"] != _VERSION
            or type(checkpoint["count"]) is not int
            or checkpoint["count"] != len(samples)
            or checkpoint["last_sha256"] != last
            or checkpoint["header_sha256"] != header["sha256"]
        ):
            raise ValueError("public market archive/checkpoint mismatch")
        return tuple(samples), last, header["sha256"]

    def append(self, sample: PublicMarketSample):
        if not isinstance(sample, PublicMarketSample):
            raise ValueError("invalid public market sample type")
        _revalidate(sample)
        if sample.session_id != self.session_id or sample.symbol != self.symbol:
            raise ValueError("public market sample/session mismatch")
        with file_lock(self._lock):
            samples, last, header = self._read_locked()
            if samples and sample.mark_start_ns <= samples[-1].funding_end_ns:
                raise ValueError("overlapping, reordered or repeated public market evidence")
            record = _seal(
                {
                    "type": "public-market-sample",
                    "version": _VERSION,
                    "sequence": len(samples) + 1,
                    "previous_sha256": last,
                    "sample": asdict(sample),
                }
            )
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                _write(fd, _canonical(record) + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            self._head_write(len(samples) + 1, record["sha256"], header)

    def samples(self) -> tuple[PublicMarketSample, ...]:
        with file_lock(self._lock):
            rows, _last, _header = self._read_locked()
            return rows
