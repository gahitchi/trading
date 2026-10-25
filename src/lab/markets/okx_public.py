"""Credential-free, read-only OKX EEA 1-minute candle ingress.

This module contains no account API, order operation, signing function or
configurable host. It validates observed bars but DOES NOT simulate fills
from them: OHLCV alone is insufficient for truthful minute execution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, decimal

_EEA_BASE = "https://eea.okx.com"
_ONE_MINUTE_MS = 60_000
_MAX_BYTES = 1_000_000


class MarketDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublicCandle:
    instrument_id: str
    start_ns: int
    end_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_native: Decimal
    source: str = "OKX_EEA_PUBLIC_CANDLES"

    def __post_init__(self) -> None:
        if type(self.start_ns) is not int or type(self.end_ns) is not int:
            raise MarketDataError("candle timestamps must be exact integer UTC nanoseconds")
        if self.start_ns < 0 or self.start_ns % 60_000_000_000:
            raise MarketDataError("one-minute candle must start on UTC minute boundary")
        if self.end_ns != self.start_ns + 60_000_000_000 or self.end_ns > MAX_EVENT_TIMESTAMP_NS:
            raise MarketDataError("one-minute candle end exceeds signed-64-bit timestamp range")
        for name in ("open", "high", "low", "close", "volume_native"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise MarketDataError(f"{name} must be an exact Decimal")
            try:
                decimal(value)
            except ValueError as exc:
                raise MarketDataError(f"nonfinite {name}") from exc
        if any(getattr(self, name) <= 0 for name in ("open", "high", "low", "close")):
            raise MarketDataError("invalid nonpositive price")
        if self.volume_native < 0:
            raise MarketDataError("negative volume")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise MarketDataError("OHLC inconsistent with bounds")
        if self.high < self.low:
            raise MarketDataError("inverted OHLC candle")


def parse_confirmed_candles(payload: dict, instrument_id: str) -> tuple[PublicCandle, ...]:
    if not re.fullmatch(r"[A-Z0-9_-]{3,80}", instrument_id):
        raise MarketDataError("instrument_id must be a canonical OKX identifier")
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise MarketDataError("OKX public data request failed")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise MarketDataError("OKX data is not a list")
    seen = set()
    output = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 9:
            raise MarketDataError("unexpected OKX candle schema")
        if row[8] not in ("0", "1"):
            raise MarketDataError("unknown OKX candle confirmation flag")
        if row[8] == "0":
            continue  # NEVER use an unfinished candle for a completed-bar signal
        try:
            # Provider timestamps and all seven numeric fields are decimal
            # strings. int(60000.9), int(True), or str(float) would fabricate
            # precision and potentially accept corrupted market observations.
            if type(row[0]) is not str or not re.fullmatch(r"[0-9]{1,19}", row[0]):
                raise MarketDataError("timestamp must be exact decimal milliseconds")
            if any(type(value) is not str for value in row[1:8]):
                raise MarketDataError("candle values must be exact decimal strings")
            start_ms = int(row[0])
            if start_ms % _ONE_MINUTE_MS:
                raise MarketDataError("timestamp not aligned to UTC minute")
            if start_ms in seen:
                raise MarketDataError("duplicate completed candle")
            seen.add(start_ms)
            # The extra quote/base-volume fields are not used for execution,
            # but an invalid provider row must not be promoted to evidence.
            fields = [decimal(value) for value in row[1:8]]
            if any(value < 0 for value in fields[4:]):
                raise MarketDataError("negative native or converted volume")
            values = fields[:5]
        except MarketDataError:
            raise
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise MarketDataError("malformed OKX candle data") from exc
        output.append(
            PublicCandle(
                instrument_id=instrument_id,
                start_ns=start_ms * 1_000_000,
                end_ns=(start_ms + _ONE_MINUTE_MS) * 1_000_000,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                volume_native=values[4],
            )
        )
    output.sort(key=lambda row: row.start_ns)
    return tuple(output)


def latest_confirmed_candles(instrument_id: str, *, limit: int = 100) -> tuple[PublicCandle, ...]:
    """Fetch public, observed candles only; no secrets, and no trading endpoints.

    Do not treat the returned volume as base-asset volume for derivative contracts.
    """
    if not re.fullmatch(r"[A-Z0-9_-]{3,80}", instrument_id):
        raise MarketDataError("instrument_id must be a canonical OKX identifier")
    if type(limit) is not int or not 1 <= limit <= 300:
        raise MarketDataError("limit must be an integer from 1 to 300")
    url = f"{_EEA_BASE}/api/v5/market/candles?{urlencode({'instId': instrument_id, 'bar': '1m', 'limit': limit})}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "trading-lab/0.2"})
    try:
        with urlopen(request, timeout=10) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            final = urlsplit(final_url)
            # urllib follows redirects by default. The original URL being
            # pinned is insufficient for a claim of EEA provider provenance.
            if final.scheme != "https" or final.netloc != "eea.okx.com" or final.path != "/api/v5/market/candles":
                raise MarketDataError("OKX EEA candle endpoint redirected outside qualified origin")
            raw = response.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise MarketDataError("OKX public feed unavailable") from exc
    if len(raw) > _MAX_BYTES:
        raise MarketDataError("OKX response too large")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MarketDataError("OKX returned invalid JSON") from exc
    return parse_confirmed_candles(payload, instrument_id)


def historical_confirmed_candles(
    instrument_id: str, *, pages: int = 5, limit: int = 100
) -> tuple[PublicCandle, ...]:
    """Bounded, rate-limited read-only historical 1m observation retrieval.

    Uses the provider's documented `after` (strictly earlier) timestamp
    cursor. Gaps are never forward-filled, revisions are never silently
    combined, and overlapping/repeated pages fail closed. This is not a
    complete historical source or a live execution/quote feed.
    """
    import time

    from lab.markets.okx_observations import _fetch

    if not re.fullmatch(r"[A-Z0-9_-]{3,80}", instrument_id):
        raise MarketDataError("instrument_id must be a canonical OKX identifier")
    if type(pages) is not int or not 1 <= pages <= 50:
        raise MarketDataError("historical pages must be 1..50")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise MarketDataError("historical page limit must be 1..100")

    cursor_ms: int | None = None
    observed: dict[int, PublicCandle] = {}
    for number in range(pages):
        params: dict[str, str | int] = {"instId": instrument_id, "bar": "1m", "limit": limit}
        if cursor_ms is not None:
            params["after"] = str(cursor_ms)
        payload, _received_ns = _fetch("/api/v5/market/history-candles", params)
        page = parse_confirmed_candles(payload, instrument_id)
        if not page:
            break
        if cursor_ms is not None and page[-1].start_ns >= cursor_ms * 1_000_000:
            raise MarketDataError("historical provider failed strictly older cursor")
        for bar in page:
            if bar.start_ns in observed:
                raise MarketDataError("overlapping historical pages or inconsistent data revision")
            observed[bar.start_ns] = bar
        oldest_ms = page[0].start_ns // 1_000_000
        if cursor_ms is not None and oldest_ms >= cursor_ms:
            raise MarketDataError("historical pagination cursor did not progress")
        cursor_ms = oldest_ms
        if len(payload["data"]) < limit:
            break
        if number + 1 < pages:
            # 20 requests / 2 seconds published for the history endpoint;
            # choose a conservative 8 requests/second instead of bursting.
            time.sleep(0.125)
    return tuple(observed[key] for key in sorted(observed))
