"""Credential-free, EEA-pinned trade and derivative mark observations.

These APIs yield *recent snapshots*, not complete tick history, live quotes,
order-book depth, guaranteed ordering, or authenticated derivatives entitlement.
Never pass their response straight to an order executor or infer a bid/ask.
"""

from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from lab.markets.domain import MarkPrice, Side, Trade, decimal
from lab.markets.okx_public import MarketDataError

_EEA_HOST = "eea.okx.com"
_BASE = "https://" + _EEA_HOST
_MAX_BYTES = 1_000_000
_INSTRUMENT = re.compile(r"[A-Z0-9_-]{3,80}")
_TRADE_ID = re.compile(r"-?[0-9]{1,128}")


def _instrument(value: str) -> str:
    if not isinstance(value, str) or not _INSTRUMENT.fullmatch(value):
        raise MarketDataError("invalid canonical OKX instrument ID")
    return value


def _response(payload: object) -> list:
    if not isinstance(payload, dict) or payload.get("code") != "0" or not isinstance(payload.get("data"), list):
        raise MarketDataError("unexpected OKX EEA public endpoint response")
    return payload["data"]


def _fetch(path: str, params: dict[str, str | int]) -> tuple[dict, int]:
    url = _BASE + path + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "trading-lab/0.2"})
    try:
        with urlopen(request, timeout=10) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            final = urlsplit(final_url)
            if final.scheme != "https" or final.hostname != _EEA_HOST:
                raise MarketDataError("OKX EEA endpoint redirected outside qualified region")
            raw = response.read(_MAX_BYTES + 1)
        observed_ns = time.time_ns()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise MarketDataError("OKX EEA public endpoint unavailable") from exc
    if len(raw) > _MAX_BYTES:
        raise MarketDataError("OKX EEA public response too large")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MarketDataError("invalid OKX EEA public JSON") from exc
    _response(payload)
    return payload, observed_ns


def parse_public_trades(payload: object, instrument_id: str, received_ns: int) -> tuple[Trade, ...]:
    """Validate and sort newest-first REST trades to monotonic event-time."""
    _instrument(instrument_id)
    if type(received_ns) is not int or received_ns < 0:
        raise MarketDataError("invalid local receive timestamp")
    observations: list[Trade] = []
    known = set()
    try:
        for row in _response(payload):
            if not isinstance(row, dict) or row.get("instId") != instrument_id:
                raise MarketDataError("trade row belongs to an unqualified instrument")
            trade_id = row["tradeId"]
            if type(trade_id) is not str or not _TRADE_ID.fullmatch(trade_id) or trade_id in known:
                raise MarketDataError("invalid or duplicated OKX trade identifier")
            known.add(trade_id)
            raw_ts = row["ts"]
            if type(raw_ts) is not str or not raw_ts.isdecimal():
                raise MarketDataError("trade timestamp must be decimal milliseconds")
            raw_price, raw_quantity = row["px"], row["sz"]
            if type(raw_price) is not str or type(raw_quantity) is not str:
                raise MarketDataError("trade price and size must be exact decimal strings")
            source = row.get("source")
            if source is not None and type(source) is not str:
                raise MarketDataError("invalid trade venue source")
            observations.append(
                Trade(
                    trade_id,
                    int(raw_ts) * 1_000_000,
                    received_ns,
                    decimal(raw_price),
                    decimal(raw_quantity),
                    taker_side=Side(row["side"]),
                    venue_source=source,
                )
            )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, MarketDataError):
            raise
        raise MarketDataError("malformed OKX trade row") from exc
    return tuple(sorted(observations, key=lambda trade: (trade.exchange_ns, int(trade.trade_id))))


def parse_public_mark(payload: object, instrument_id: str, received_ns: int) -> MarkPrice:
    """Mark/index valuation is a separate feed from last-trade execution events."""
    _instrument(instrument_id)
    if not instrument_id.endswith("-SWAP"):
        raise MarketDataError("mark endpoint requires a swap instrument ID")
    if type(received_ns) is not int or received_ns < 0:
        raise MarketDataError("invalid local receive timestamp")
    rows = _response(payload)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise MarketDataError("mark endpoint must return precisely one instrument")
    row = rows[0]
    if row.get("instId") != instrument_id or row.get("instType") != "SWAP":
        raise MarketDataError("mark belongs to an unexpected instrument or contract type")
    try:
        raw_ts = row["ts"]
        raw_price = row["markPx"]
        if type(raw_ts) is not str or not raw_ts.isdecimal() or type(raw_price) is not str:
            raise MarketDataError("mark time/price schema invalid")
        stamp = int(raw_ts) * 1_000_000
        return MarkPrice(f"{instrument_id}:{raw_ts}", stamp, received_ns, decimal(raw_price))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, MarketDataError):
            raise
        raise MarketDataError("malformed OKX mark record") from exc


def recent_public_trades(instrument_id: str, *, limit: int = 100) -> tuple[Trade, ...]:
    _instrument(instrument_id)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise MarketDataError("OKX recent trade limit must be 1..500")
    response, received_ns = _fetch("/api/v5/market/trades", {"instId": instrument_id, "limit": limit})
    return parse_public_trades(response, instrument_id, received_ns)


def current_public_mark(instrument_id: str) -> MarkPrice:
    _instrument(instrument_id)
    response, received_ns = _fetch("/api/v5/public/mark-price", {"instType": "SWAP", "instId": instrument_id})
    return parse_public_mark(response, instrument_id, received_ns)
