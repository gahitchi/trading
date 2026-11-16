"""Forward-only, read-only public USD-M closed-minute price receipts.

A REST response is first available at local receipt completion, never at its
kline close timestamp. This recorder does not authenticate the provider,
establish complete market history, authorize orders, or generate returns.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from lab.bearish_hybrid import MINUTE_NS, Bar
from lab.bearish_hybrid_acquisition import _https_get, _immutably_save
from lab.bearish_hybrid_public_capture import MAX_REQUEST_NS, _validate_symbol
from lab.persistence import parse_json

BASE = "https://fapi.binance.com"
MAX_RESPONSE_BYTES = 8_192
MAX_RECEIPTS = 200_000
MAX_PRICE_AGE_NS = 2 * MINUTE_NS


@dataclass(frozen=True)
class PriceCapture:
    """Locally received closed bar; NOT an attested market-data source."""

    symbol: str
    requested_ns: int
    received_ns: int
    raw_sha256: str
    source: str
    bar: Bar


def _url(symbol: str) -> str:
    _validate_symbol(symbol)
    return f"{BASE}/fapi/v1/klines?symbol={symbol}&interval=1m&limit=2"


def _decimal(value: object, name: str) -> float:
    if type(value) is not str or len(value) > 64:
        raise ValueError(f"invalid kline {name}")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid kline {name}") from exc
    if not isfinite(parsed) or parsed < 0:
        raise ValueError(f"invalid kline {name}")
    return parsed


def _closed_bar(raw: bytes, requested_ns: int, received_ns: int) -> Bar:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise ValueError("invalid or oversized public kline response")
    try:
        decoded = parse_json(raw)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid public kline JSON") from exc
    if type(decoded) is not list or not 1 <= len(decoded) <= 2:
        raise ValueError("expected one or two Binance 1m klines")
    previous_open_ns = -1
    selected = None
    for item in decoded:
        if type(item) is not list or len(item) != 12:
            raise ValueError("expected twelve fields in each Binance kline")
        opening, closing, trades = item[0], item[6], item[8]
        if (
            type(opening) is not int
            or opening <= 0
            or opening % 60_000
            or type(closing) is not int
            or closing != opening + 59_999
            or type(trades) is not int
            or trades < 0
        ):
            raise ValueError("invalid kline timestamps or trade count")
        opening_ns = opening * 1_000_000
        if opening_ns <= previous_open_ns or opening_ns > received_ns:
            raise ValueError("out-of-order or future public kline")
        if previous_open_ns > 0 and opening_ns - previous_open_ns != MINUTE_NS:
            raise ValueError("nonconsecutive Binance kline response")
        previous_open_ns = opening_ns
        volume = _decimal(item[5], "base volume")
        taker_base = _decimal(item[9], "taker buy base volume")
        if taker_base > volume:
            raise ValueError("taker base volume exceeds total")
        bar = Bar(
            end_ns=opening_ns + MINUTE_NS,
            open=_decimal(item[1], "open"),
            high=_decimal(item[2], "high"),
            low=_decimal(item[3], "low"),
            close=_decimal(item[4], "close"),
            quote_volume=_decimal(item[7], "quote volume"),
            taker_buy_quote=_decimal(item[10], "taker buy quote volume"),
        )
        bar.check()
        _decimal(item[11], "unused value")
        # A candle closing *during* a request is not accepted for this
        # request. Only use a candle already closed when the GET started.
        if bar.end_ns <= requested_ns:
            selected = bar
    if selected is None or not 0 <= received_ns - selected.end_ns <= MAX_PRICE_AGE_NS:
        raise ValueError("missing or stale completed public minute")
    return selected


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def record_forward_price(
    root: Path,
    symbol: str,
    *,
    source: Callable[[str], bytes] | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
) -> PriceCapture:
    """Make one bounded keyless public GET; never write an order.

    Injected sources are labeled test-only and do not imply venue provenance.
    """
    url = _url(symbol)
    requested_ns = clock_ns()
    if type(requested_ns) is not int or requested_ns <= 0:
        raise ValueError("invalid price request clock")
    raw = _https_get(url, MAX_RESPONSE_BYTES) if source is None else source(url)
    received_ns = clock_ns()
    if (
        type(received_ns) is not int
        or received_ns < requested_ns
        or received_ns - requested_ns > MAX_REQUEST_NS
    ):
        raise ValueError("unreliable price receive clock")
    bar = _closed_bar(raw, requested_ns, received_ns)
    identity = "binance-public-https" if source is None else "injected-test-source"
    record = {
        "version": 1,
        "venue": "binance-usdm",
        "symbol": symbol,
        "url": url,
        "requested_ns": requested_ns,
        "received_ns": received_ns,
        "raw_sha256": _sha(raw),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "source": identity,
        "selected_end_ns": bar.end_ns,
        "independently_authenticated": False,
        "historical_publication_verified": False,
        "price_history_complete": False,
        "qualified": False,
        "actual_notional": 0,
    }
    location = Path(root) / "binance-usdm" / "forward-prices" / symbol / f"{received_ns}.json"
    if location.exists() or location.is_symlink():
        raise ValueError("duplicate immutable price capture timestamp")
    _immutably_save(location, (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return PriceCapture(symbol, requested_ns, received_ns, record["raw_sha256"], identity, bar)


def load_forward_prices(root: Path, symbol: str) -> tuple[PriceCapture, ...]:
    """Offline-only hash and schema verification. Gaps are never backfilled."""
    _url(symbol)
    folder = Path(root) / "binance-usdm" / "forward-prices" / symbol
    if folder.is_symlink() or any(parent.is_symlink() for parent in folder.parents):
        raise ValueError("untrusted forward price directory symlink")
    if not folder.exists():
        return ()
    paths = sorted(folder.iterdir())
    if len(paths) > MAX_RECEIPTS:
        raise ValueError("too many public price receipts")
    captures = []
    previous_received = -1
    previous_bar = None
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.suffix != ".json" or path.stat().st_size > 24_000:
            raise ValueError("unexpected public price receipt file")
        try:
            saved = parse_json(path.read_bytes())
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ValueError("invalid saved public price receipt") from exc
        expected_fields = {
            "version", "venue", "symbol", "url", "requested_ns", "received_ns",
            "raw_sha256", "raw_base64", "source", "selected_end_ns",
            "independently_authenticated", "historical_publication_verified",
            "price_history_complete", "qualified", "actual_notional",
        }
        if type(saved) is not dict or set(saved) != expected_fields:
            raise ValueError("unrecognized public price receipt schema")
        start, end = saved["requested_ns"], saved["received_ns"]
        if (
            type(start) is not int
            or type(end) is not int
            or start <= 0
            or end <= previous_received
            or end < start
            or end - start > MAX_REQUEST_NS
            or path.stem != str(end)
            or type(saved["version"]) is not int
            or saved["version"] != 1
            or saved["venue"] != "binance-usdm"
            or saved["symbol"] != symbol
            or saved["url"] != _url(symbol)
            or saved["source"] not in ("binance-public-https", "injected-test-source")
            or saved["independently_authenticated"] is not False
            or saved["historical_publication_verified"] is not False
            or saved["price_history_complete"] is not False
            or saved["qualified"] is not False
            or type(saved["actual_notional"]) is not int
            or saved["actual_notional"] != 0
        ):
            raise ValueError("untrusted or nonchronological public price receipt")
        encoded = saved["raw_base64"]
        if type(encoded) is not str or len(encoded) > 2 * MAX_RESPONSE_BYTES:
            raise ValueError("invalid encoded public price")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("invalid encoded public price") from exc
        if type(saved["raw_sha256"]) is not str or _sha(raw) != saved["raw_sha256"]:
            raise ValueError("modified public price response")
        bar = _closed_bar(raw, start, end)
        if type(saved["selected_end_ns"]) is not int or saved["selected_end_ns"] != bar.end_ns:
            raise ValueError("modified selected public minute")
        if previous_bar is not None and (
            bar.end_ns < previous_bar.end_ns
            or (bar.end_ns == previous_bar.end_ns and bar != previous_bar)
        ):
            raise ValueError("reordered or revised completed public minute")
        captures.append(PriceCapture(symbol, start, end, _sha(raw), saved["source"], bar))
        previous_received = end
        previous_bar = bar
    return tuple(captures)
