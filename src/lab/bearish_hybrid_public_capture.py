"""Forward-only, keyless public USD-M feature receipt capture.

The event timestamp on a funding settlement or OI measurement is *not* a
publication timestamp. The conservative earliest-replayable availability is
when this process finished receiving the HTTP response. Stored receipts are
content-pinned but NOT independently signed/authenticated, and never by
itself qualify a short trade or an historical strategy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from lab.bearish_hybrid import HOUR_NS, MINUTE_NS, FundingEvent, OIObservation
from lab.bearish_hybrid_acquisition import _https_get, _immutably_save
from lab.persistence import parse_json

BASE = "https://fapi.binance.com"
MAX_RESPONSE_BYTES = 65_536
MAX_REQUEST_NS = 60_000_000_000
KINDS = ("open-interest", "settled-funding")


@dataclass(frozen=True)
class FeatureCapture:
    """Read-only evidence; source is not independently certified or signed."""

    symbol: str
    kind: str
    requested_ns: int
    received_ns: int
    raw_sha256: str
    source: str
    payload: object


def _validate_symbol(symbol: str) -> None:
    if (
        type(symbol) is not str
        or re.fullmatch(r"[A-Z0-9]{5,20}", symbol) is None
        or not symbol.endswith("USDT")
        or symbol == "BTCUSDT"
    ):
        raise ValueError("expected a non-BTC USD-M USDT symbol")


def _url(symbol: str, kind: str) -> str:
    _validate_symbol(symbol)
    if kind == "open-interest":
        return f"{BASE}/fapi/v1/openInterest?symbol={symbol}"
    if kind == "settled-funding":
        return f"{BASE}/fapi/v1/fundingRate?symbol={symbol}&limit=100"
    raise ValueError("unsupported public feature")


def _integer_ms(value: object, label: str, received_ns: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {label} event timestamp")
    event_ns = value * 1_000_000
    if event_ns > received_ns:
        raise ValueError(f"future {label} event timestamp")
    return event_ns


def _finite_decimal(value: object, label: str, *, positive: bool = False) -> float:
    if type(value) is not str or len(value) > 64:
        raise ValueError(f"invalid {label} decimal")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label} decimal") from exc
    if not isfinite(result) or (positive and result <= 0):
        raise ValueError(f"invalid {label} decimal")
    return result


def _payload(raw: bytes, kind: str, symbol: str, received_ns: int) -> object:
    """Validate values without inferring an earlier historical publish time."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise ValueError("oversized or invalid public feature response")
    try:
        decoded = parse_json(raw)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid public feature JSON") from exc
    if kind == "open-interest":
        if type(decoded) is not dict or decoded.get("symbol") != symbol:
            raise ValueError("wrong open-interest response symbol")
        _integer_ms(decoded.get("time"), "OI measurement", received_ns)
        _finite_decimal(decoded.get("openInterest"), "open interest", positive=True)
    elif kind == "settled-funding":
        if type(decoded) is not list or len(decoded) > 100:
            raise ValueError("invalid funding history response")
        last = 0
        for event in decoded:
            if type(event) is not dict or event.get("symbol") != symbol:
                raise ValueError("wrong funding history response symbol")
            settlement = _integer_ms(event.get("fundingTime"), "funding settlement", received_ns)
            if settlement <= last:
                raise ValueError("unordered or duplicate funding settlements")
            rate = _finite_decimal(event.get("fundingRate"), "settled funding rate")
            if not -1 < rate < 1:
                raise ValueError("invalid settled funding rate range")
            last = settlement
    else:
        raise ValueError("unsupported public feature")
    return decoded


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def record_public_feature(
    root: Path,
    symbol: str,
    kind: str,
    *,
    source: Callable[[str], bytes] | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
) -> FeatureCapture:
    """Capture exactly one public GET; no broker credentials or historical queries.

    The source clock is called once BEFORE the request, once AFTER receiving
    every byte. The latter is the conservative first-observed timestamp;
    storing an earlier fundingTime/OI sample time does NOT backdate access.
    An injected source is explicitly labeled as such and never attested.
    """
    url = _url(symbol, kind)
    requested_ns = clock_ns()
    if type(requested_ns) is not int or requested_ns <= 0:
        raise ValueError("invalid request clock")
    if source is None:
        raw = _https_get(url, MAX_RESPONSE_BYTES)
        identity = "binance-public-https"
    else:
        raw = source(url)
        identity = "injected-test-source"
    received_ns = clock_ns()
    if type(received_ns) is not int or received_ns < requested_ns or received_ns - requested_ns > MAX_REQUEST_NS:
        raise ValueError("unreliable feature receive clock")
    parsed = _payload(raw, kind, symbol, received_ns)
    record = {
        "version": 1,
        "venue": "binance-usdm",
        "symbol": symbol,
        "kind": kind,
        "url": url,
        "requested_ns": requested_ns,
        "received_ns": received_ns,
        "raw_sha256": _sha(raw),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "source": identity,
        "independently_authenticated": False,
        "historical_publication_verified": False,
        "funding_history_complete": False,
    }
    location = Path(root) / "binance-usdm" / "forward-features" / symbol / kind / f"{received_ns}.json"
    if location.exists() or location.is_symlink():
        raise ValueError("duplicate immutable feature capture timestamp")
    _immutably_save(location, (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return FeatureCapture(symbol, kind, requested_ns, received_ns, record["raw_sha256"], identity, parsed)


def load_public_features(root: Path, symbol: str, kind: str) -> tuple[FeatureCapture, ...]:
    """Revalidate immutable raw capture receipts without touching the network.

    Only files created by this local recorder are accepted as evidence. A
    hash/HTTPS source label cannot attest origin or historical availability.
    """
    _url(symbol, kind)
    folder = Path(root) / "binance-usdm" / "forward-features" / symbol / kind
    if folder.is_symlink() or any(p.is_symlink() for p in folder.parents):
        raise ValueError("untrusted feature evidence directory symlink")
    if not folder.exists():
        return ()
    paths = sorted(folder.iterdir())
    if len(paths) > 200_000:
        raise ValueError("too many feature receipt files")
    captures = []
    previous = -1
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise ValueError("unexpected feature receipt file")
        if path.stat().st_size > 150_000:
            raise ValueError("oversized feature receipt")
        try:
            record = parse_json(path.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid recorded feature receipt JSON") from exc
        if type(record) is not dict or set(record) != {
            "version",
            "venue",
            "symbol",
            "kind",
            "url",
            "requested_ns",
            "received_ns",
            "raw_sha256",
            "raw_base64",
            "source",
            "independently_authenticated",
            "historical_publication_verified",
            "funding_history_complete",
        }:
            raise ValueError("unrecognized feature receipt schema")
        t = record["received_ns"]
        start = record["requested_ns"]
        if (
            type(t) is not int
            or t <= previous
            or t <= 0
            or path.stem != str(t)
            or type(start) is not int
            or start <= 0
            or t < start
            or t - start > MAX_REQUEST_NS
            or record["version"] != 1
            or type(record["version"]) is not int
            or record["venue"] != "binance-usdm"
            or record["symbol"] != symbol
            or record["kind"] != kind
            or record["url"] != _url(symbol, kind)
            or record["source"] not in ("binance-public-https", "injected-test-source")
            or record["independently_authenticated"] is not False
            or record["historical_publication_verified"] is not False
            or record["funding_history_complete"] is not False
        ):
            raise ValueError("untrusted or out-of-order feature receipt")
        raw_b64 = record["raw_base64"]
        if type(raw_b64) is not str or len(raw_b64) > MAX_RESPONSE_BYTES * 2:
            raise ValueError("invalid encoded feature payload")
        try:
            raw = base64.b64decode(raw_b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("invalid encoded feature payload") from exc
        if type(record["raw_sha256"]) is not str or _sha(raw) != record["raw_sha256"]:
            raise ValueError("modified feature response payload")
        captures.append(
            FeatureCapture(symbol, kind, start, t, _sha(raw), record["source"], _payload(raw, kind, symbol, t))
        )
        previous = t
    return tuple(captures)


def first_seen_oi(captures: Sequence[FeatureCapture], *, decision_ns: int) -> OIObservation | None:
    """Compare distinct, recently measured OI samples first received by decision.

    A newer HTTP receipt is NOT evidence of a new OI measurement. Repeated
    nominal measurement times cannot fabricate an OI move or refresh its
    availability age. Received timestamps remain the signal's as-of clocks.
    """
    if type(decision_ns) is not int or decision_ns <= 0 or decision_ns % MINUTE_NS:
        raise ValueError("decision requires completed UTC minute")
    # Conservative freshness policy, NOT a venue publication-time guarantee.
    max_sample_lag_ns = 2 * MINUTE_NS
    eligible: list[tuple[FeatureCapture, int, float]] = []
    last_received = -1
    last_measured = -1
    last_value = 0.0
    symbol: str | None = None
    for snapshot in captures:
        if not isinstance(snapshot, FeatureCapture):
            raise ValueError("expected open-interest captures")
        if type(snapshot.received_ns) is not int or snapshot.received_ns <= last_received:
            raise ValueError("nonchronological open-interest captures")
        last_received = snapshot.received_ns
        if snapshot.received_ns > decision_ns:
            break  # No future source contents may affect this decision.
        if snapshot.kind != "open-interest":
            raise ValueError("expected open-interest captures")
        if symbol is None:
            symbol = snapshot.symbol
        elif symbol != snapshot.symbol:
            raise ValueError("mixed-symbol open-interest captures")
        if type(snapshot.payload) is not dict or snapshot.payload.get("symbol") != symbol:
            raise ValueError("invalid open-interest capture payload")
        measured = _integer_ms(snapshot.payload.get("time"), "OI measurement", snapshot.received_ns)
        value = _finite_decimal(snapshot.payload.get("openInterest"), "open interest", positive=True)
        if measured < last_measured:
            raise ValueError("out-of-order open-interest measurements")
        if measured == last_measured:
            if value != last_value:
                raise ValueError("conflicting revisions to open-interest measurement")
            continue  # Re-polling an unchanged event never refreshes its age.
        last_measured, last_value = measured, value
        if snapshot.received_ns - measured <= max_sample_lag_ns:
            eligible.append((snapshot, measured, value))
    if len(eligible) < 2 or decision_ns - eligible[-1][0].received_ns > 60 * MINUTE_NS:
        return None
    current, current_measurement, current_value = eligible[-1]
    for previous, prior_measurement, prior_value in reversed(eligible[:-1]):
        receive_gap = current.received_ns - previous.received_ns
        sample_gap = current_measurement - prior_measurement
        if receive_gap >= 5 * MINUTE_NS and sample_gap >= 5 * MINUTE_NS:
            if receive_gap > 60 * MINUTE_NS or sample_gap > 60 * MINUTE_NS:
                return None
            return OIObservation(
                previous_ns=previous.received_ns,
                previous_value=prior_value,
                observed_ns=current.received_ns,
                value=current_value,
            )
    return None


def first_seen_funding(captures: Sequence[FeatureCapture], *, decision_ns: int) -> tuple[FundingEvent, ...]:
    """Show only settlements first received by decision time.

    This returns conservative known-at bounds but explicitly DOES NOT prove
    event completeness or set a funding_verified=True flag. A rate with a
    historic settlement must never leak into decisions made before receipt.
    """
    if type(decision_ns) is not int or decision_ns <= 0 or decision_ns % MINUTE_NS:
        raise ValueError("decision requires completed UTC minute")
    seen: dict[int, FundingEvent] = {}
    last = -1
    symbol: str | None = None
    for snapshot in captures:
        if not isinstance(snapshot, FeatureCapture):
            raise ValueError("expected settled-funding captures")
        if snapshot.received_ns <= last:
            raise ValueError("nonchronological funding captures")
        last = snapshot.received_ns
        if snapshot.received_ns > decision_ns:
            break  # Never validate future funding content before it is received.
        if snapshot.kind != "settled-funding":
            raise ValueError("expected settled-funding captures")
        if symbol is None:
            symbol = snapshot.symbol
        elif symbol != snapshot.symbol:
            raise ValueError("mixed-symbol funding captures")
        for item in snapshot.payload:
            settled = item["fundingTime"] * 1_000_000
            rate = float(item["fundingRate"])
            original = seen.get(settled)
            if original is None:
                seen[settled] = FundingEvent(settled, snapshot.received_ns, rate)
            elif original.rate != rate:
                raise ValueError("conflicting revisions to first-observed funding settlement")
    return tuple(event for key, event in sorted(seen.items()) if key > decision_ns - 72 * HOUR_NS)
