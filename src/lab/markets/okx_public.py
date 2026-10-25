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
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from lab.markets.domain import decimal

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
        if self.start_ns < 0 or self.start_ns % 60_000_000_000:
            raise MarketDataError("one-minute candle must start on UTC minute boundary")
        if self.end_ns != self.start_ns + 60_000_000_000:
            raise MarketDataError("one-minute candle has invalid end")
        for name in ("open", "high", "low", "close"):
            if decimal(getattr(self, name)) <= 0:
                raise MarketDataError("invalid nonpositive price")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise MarketDataError("OHLC inconsistent with bounds")
        if self.high < self.low or self.volume_native < 0:
            raise MarketDataError("negative volume or inverted candle")


def parse_confirmed_candles(payload: dict, instrument_id: str) -> tuple[PublicCandle, ...]:
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise MarketDataError("OKX public data request failed")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise MarketDataError("OKX data is not a list")
    seen = set()
    output = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            raise MarketDataError("unexpected OKX candle schema")
        if row[8] not in ("0", "1"):
            raise MarketDataError("unknown OKX candle confirmation flag")
        if row[8] == "0":
            continue  # NEVER use an unfinished candle for a completed-bar signal
        try:
            start_ms = int(row[0])
            if start_ms < 0 or start_ms % _ONE_MINUTE_MS:
                raise MarketDataError("timestamp not aligned to UTC minute")
            if start_ms in seen:
                raise MarketDataError("duplicate completed candle")
            seen.add(start_ms)
            values = [decimal(str(x)) for x in row[1:6]]
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
