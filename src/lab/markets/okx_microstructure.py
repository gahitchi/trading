"""Read-only OKX EEA book and indicative funding snapshots.

REST order-book snapshots are not an incremental, gap-free L2 feed. Quotes may
be crossed during pre-open and cannot prove passive maker fills or queue rank.
The public funding-rate endpoint reports an *indication*, NOT a realized
account transaction. Never use it to settle balances.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, decimal
from lab.markets.okx_observations import _fetch, _instrument, _response
from lab.markets.okx_public import MarketDataError

_MS_NS = 1_000_000
_ASCII_MS = re.compile(r"[0-9]{1,19}")


def _time_ms(value: object, label: str) -> int:
    if type(value) is not str or not _ASCII_MS.fullmatch(value):
        raise MarketDataError(f"{label} must be integer milliseconds as an ASCII string")
    timestamp = int(value) * _MS_NS
    if timestamp > MAX_EVENT_TIMESTAMP_NS:
        raise MarketDataError(f"{label} exceeds signed-64-bit UTC nanoseconds")
    return timestamp


def _receive_time(received_ns: int, exchange_ns: int) -> None:
    if type(received_ns) is not int or not exchange_ns <= received_ns <= MAX_EVENT_TIMESTAMP_NS:
        raise MarketDataError("invalid order-book/funding exchange and receive timestamps")


def _exact_decimal(value: object, label: str) -> Decimal:
    if type(value) is not str:
        raise MarketDataError(f"{label} must be an exact decimal string")
    try:
        return decimal(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise MarketDataError(f"invalid {label} decimal") from exc


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size_native: Decimal
    order_count: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    instrument_id: str
    exchange_ns: int
    received_ns: int
    sequence_id: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    source: str = "OKX_EEA_PUBLIC_REST_BOOK"

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    def crossed_or_locked(self) -> bool:
        return self.best_ask <= self.best_bid

    @property
    def spread(self) -> Decimal | None:
        # A pre-open crossed/locked book is not an executable spread.
        return None if self.crossed_or_locked else self.best_ask - self.best_bid


@dataclass(frozen=True, slots=True)
class FundingIndication:
    instrument_id: str
    exchange_ns: int
    received_ns: int
    indicated_rate: Decimal
    funding_time_ns: int
    next_funding_time_ns: int
    source: str = "OKX_EEA_PUBLIC_INDICATIVE_FUNDING"
    realized_payment_qualified: bool = False


def _levels(value: object, *, ask: bool, depth: int) -> tuple[BookLevel, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= depth:
        raise MarketDataError("invalid or empty public order-book depth")
    output = []
    last_price: Decimal | None = None
    for row in value:
        if not isinstance(row, list) or len(row) != 4:
            raise MarketDataError("unexpected public order-book level schema")
        price = _exact_decimal(row[0], "book price")
        size = _exact_decimal(row[1], "book size")
        if price <= 0 or size <= 0:
            raise MarketDataError("nonpositive book price/size")
        if type(row[2]) is not str or not row[2].isascii() or not row[2].isdecimal():
            raise MarketDataError("invalid deprecated book level field")
        if type(row[3]) is not str or not re.fullmatch(r"[0-9]{1,10}", row[3]):
            raise MarketDataError("invalid level order count")
        count = int(row[3])
        if last_price is not None and ((ask and price <= last_price) or (not ask and price >= last_price)):
            raise MarketDataError("book levels are unordered or duplicated")
        last_price = price
        output.append(BookLevel(price, size, count))
    return tuple(output)


def parse_public_book(payload: object, instrument_id: str, received_ns: int, *, depth: int = 5) -> BookSnapshot:
    """Validate one REST snapshot, preserving provider native size units."""
    _instrument(instrument_id)
    if type(depth) is not int or not 1 <= depth <= 50:
        raise MarketDataError("snapshot depth must be 1..50")
    rows = _response(payload)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise MarketDataError("order-book endpoint must return exactly one snapshot")
    row = rows[0]
    try:
        exchange_ns = _time_ms(row["ts"], "book timestamp")
        _receive_time(received_ns, exchange_ns)
        sequence = row["seqId"]
        if type(sequence) is not int or sequence < 0 or sequence >= 2**63:
            raise MarketDataError("invalid REST book sequence")
        return BookSnapshot(
            instrument_id,
            exchange_ns,
            received_ns,
            sequence,
            _levels(row["bids"], ask=False, depth=depth),
            _levels(row["asks"], ask=True, depth=depth),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, MarketDataError):
            raise
        raise MarketDataError("malformed OKX public order book") from exc


def parse_public_funding(payload: object, instrument_id: str, received_ns: int) -> FundingIndication:
    """Indicated public funding rate only; never evidence of a charged payment."""
    _instrument(instrument_id)
    if not instrument_id.endswith("-SWAP"):
        raise MarketDataError("funding endpoint requires a swap instrument")
    rows = _response(payload)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise MarketDataError("funding endpoint must return exactly one instrument")
    row = rows[0]
    if row.get("instType") != "SWAP" or row.get("instId") != instrument_id:
        raise MarketDataError("funding observation belongs to an unexpected contract")
    try:
        exchange_ns = _time_ms(row["ts"], "funding observation timestamp")
        _receive_time(received_ns, exchange_ns)
        future = _time_ms(row["fundingTime"], "funding time")
        next_future = _time_ms(row["nextFundingTime"], "next funding time")
        if future <= 0 or next_future <= future:
            raise MarketDataError("invalid funding interval ordering")
        rate = _exact_decimal(row["fundingRate"], "indicated funding rate")
        if abs(rate) > Decimal("1"):
            raise MarketDataError("implausible indicative funding rate")
        return FundingIndication(instrument_id, exchange_ns, received_ns, rate, future, next_future)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, MarketDataError):
            raise
        raise MarketDataError("malformed OKX public funding observation") from exc


def current_public_book(instrument_id: str, *, depth: int = 5) -> BookSnapshot:
    _instrument(instrument_id)
    if type(depth) is not int or not 1 <= depth <= 50:
        raise MarketDataError("snapshot depth must be 1..50")
    payload, observed_ns = _fetch("/api/v5/market/books", {"instId": instrument_id, "sz": depth})
    return parse_public_book(payload, instrument_id, observed_ns, depth=depth)


def current_public_funding(instrument_id: str) -> FundingIndication:
    _instrument(instrument_id)
    payload, observed_ns = _fetch("/api/v5/public/funding-rate", {"instId": instrument_id})
    return parse_public_funding(payload, instrument_id, observed_ns)
