"""Offline-only validation of OKX `books` WebSocket replay frames.

The REST book endpoint does not establish a continuous book. This module
accepts explicit *recorded* snapshot + incremental WS frames and checks
sequence continuity without relying on the checksum field (deprecated June
2026). A validated sequence is not authenticated exchange provenance,
market-data completeness, queue priority or authority to place orders.

The bounded depth-consumption estimate is an *upper bound on displayed
participating liquidity*, not a fill simulator or an execution agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, Instrument, Side, decimal
from lab.markets.okx_microstructure import BookLevel
from lab.markets.okx_public import MarketDataError

_INT = re.compile(r"[0-9]{1,19}")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_MAX_DEPTH = 400


@dataclass(frozen=True, slots=True)
class SequencedBook:
    instrument_id: str
    seq_id: int
    exchange_ns: int
    received_ns: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    source: str = "offline_recorded_okx_books_ws"
    execution_qualified: bool = False

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price


@dataclass(frozen=True, slots=True)
class DisplayedLiquidityBound:
    requested: Decimal
    bounded_quantity: Decimal
    unfilled_quantity: Decimal
    average_displayed_price: Decimal | None
    source_sequence: int
    source: str = "offline_book_participation_estimate"
    executable_fill_verified: bool = False
    execution_authorized: bool = False


def _unsigned_ns_ms(raw: object) -> int:
    if type(raw) is not str or not _INT.fullmatch(raw):
        raise MarketDataError("book timestamp must be unsigned ASCII milliseconds")
    ns = int(raw) * 1_000_000
    if ns > MAX_EVENT_TIMESTAMP_NS:
        raise MarketDataError("book timestamp exceeds 64-bit nanoseconds")
    return ns


def _decimal_string(raw: object, name: str) -> Decimal:
    if type(raw) is not str or not _NUMBER.fullmatch(raw):
        raise MarketDataError(f"invalid book {name} decimal")
    return decimal(raw)


def _updates(rows: object, name: str) -> dict[Decimal, BookLevel | None]:
    if type(rows) is not list or len(rows) > _MAX_DEPTH:
        raise MarketDataError("invalid book update depth")
    changes: dict[Decimal, BookLevel | None] = {}
    for row in rows:
        if type(row) is not list or len(row) != 4:
            raise MarketDataError("invalid book level update")
        price = _decimal_string(row[0], "price")
        size = _decimal_string(row[1], "size")
        if price <= 0 or price in changes:
            raise MarketDataError("book price must be positive and unique per update")
        if type(row[2]) is not str or not _INT.fullmatch(row[2]):
            raise MarketDataError("invalid deprecated book level placeholder")
        if type(row[3]) is not str or not _INT.fullmatch(row[3]):
            raise MarketDataError("invalid book level order count")
        count = int(row[3])
        if size == 0:
            if count != 0:
                raise MarketDataError("deleted book level must have zero order count")
            changes[price] = None
        else:
            if count <= 0:
                raise MarketDataError("positive book level needs positive order count")
            changes[price] = BookLevel(price, size, count)
    return changes


def _sequence(raw: object, name: str, *, snapshot: bool = False) -> int:
    if type(raw) is not int or raw < (-1 if snapshot else 0) or raw >= 2**63:
        raise MarketDataError(f"invalid {name} book sequence")
    return raw


def apply_recorded_book_frame(
    previous: SequencedBook | None,
    frame: object,
    *,
    received_ns: int,
) -> SequencedBook:
    """Build a read-only book from a WS snapshot and strictly chained updates.

    Any gap or out-of-order sequence rejects the tape. Restart at a new
    independently recorded snapshot; never bridge gaps with a REST snapshot.
    """
    if type(received_ns) is not int or not 0 <= received_ns <= MAX_EVENT_TIMESTAMP_NS:
        raise MarketDataError("invalid book receipt timestamp")
    if type(frame) is not dict or type(frame.get("arg")) is not dict:
        raise MarketDataError("invalid book WebSocket envelope")
    arg = frame["arg"]
    if arg.get("channel") != "books" or type(arg.get("instId")) is not str:
        raise MarketDataError("only standard OKX books channel is supported")
    instrument_id = arg["instId"]
    if not re.fullmatch(r"[A-Z0-9_-]{3,80}", instrument_id):
        raise MarketDataError("invalid book instrument")
    action = frame.get("action")
    if action not in ("snapshot", "update"):
        raise MarketDataError("invalid book action")
    rows = frame.get("data")
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise MarketDataError("book frame requires exactly one data row")
    data = rows[0]
    checksum = data.get("checksum")
    if checksum is not None and (type(checksum) is not int or checksum != 0):
        raise MarketDataError("deprecated OKX checksum cannot validate this replay")
    stamp = _unsigned_ns_ms(data.get("ts"))
    if stamp > received_ns:
        raise MarketDataError("future book source timestamp")
    seq = _sequence(data.get("seqId"), "current")
    prev_seq = _sequence(data.get("prevSeqId"), "previous", snapshot=True)
    bids = _updates(data.get("bids"), "bids")
    asks = _updates(data.get("asks"), "asks")

    if action == "snapshot":
        if previous is not None or prev_seq != -1:
            raise MarketDataError("book snapshot must start a new replay epoch")
        if not bids or not asks or None in bids.values() or None in asks.values():
            raise MarketDataError("snapshot requires nonempty positive bid/ask levels")
        old_bids: dict[Decimal, BookLevel] = {}
        old_asks: dict[Decimal, BookLevel] = {}
    else:
        if previous is None or previous.instrument_id != instrument_id:
            raise MarketDataError("book update without matching snapshot")
        if received_ns <= previous.received_ns or stamp < previous.exchange_ns:
            raise MarketDataError("book observation regressed in time")
        if prev_seq != previous.seq_id:
            raise MarketDataError("book sequence gap or replay")
        if seq == prev_seq and (bids or asks):
            raise MarketDataError("same-sequence heartbeat cannot alter book")
        if seq < prev_seq:
            raise MarketDataError("book sequence regressed")
        old_bids = {level.price: level for level in previous.bids}
        old_asks = {level.price: level for level in previous.asks}

    for target, changes in ((old_bids, bids), (old_asks, asks)):
        for price, level in changes.items():
            if level is None:
                target.pop(price, None)
            else:
                target[price] = level
    ordered_bids = tuple(sorted(old_bids.values(), key=lambda x: x.price, reverse=True)[:_MAX_DEPTH])
    ordered_asks = tuple(sorted(old_asks.values(), key=lambda x: x.price)[:_MAX_DEPTH])
    if not ordered_bids or not ordered_asks or ordered_bids[0].price >= ordered_asks[0].price:
        raise MarketDataError("empty, locked or crossed sequenced book")
    return SequencedBook(instrument_id, seq, stamp, received_ns, ordered_bids, ordered_asks)


def estimate_displayed_taker_capacity(
    book: SequencedBook,
    instrument: Instrument,
    side: Side,
    requested: Decimal,
    *,
    max_participation: Decimal = Decimal("0.05"),
) -> DisplayedLiquidityBound:
    """Estimate bounded *displayed* taker capacity without emitting a fill.

    This is deliberately not a simulator: book snapshots do not prove future
    available liquidity, maker queue priority, regional product permission,
    latency, realized transaction costs or actual exchange settlement.
    """
    if type(book) is not SequencedBook or book.execution_qualified:
        raise MarketDataError("only unqualified validated offline sequenced books are supported")
    if instrument.symbol != book.instrument_id or type(side) is not Side:
        raise MarketDataError("book contract or side mismatch")
    if type(requested) is not Decimal or type(max_participation) is not Decimal:
        raise TypeError("capacity quantities and participation must be exact Decimal")
    decimal(requested)
    decimal(max_participation)
    instrument.check_quantity(requested)
    if not Decimal("0") < max_participation <= Decimal("1"):
        raise ValueError("participation must be in (0, 1]")
    levels = book.asks if side is Side.BUY else book.bids
    remaining = requested
    total_cost = Decimal("0")
    filled = Decimal("0")
    for level in levels:
        instrument.check_price(level.price)
        available = (level.size_native * max_participation / instrument.quantity_step).to_integral_value(
            rounding=ROUND_FLOOR
        ) * instrument.quantity_step
        qty = min(remaining, available)
        total_cost += qty * level.price
        filled += qty
        remaining -= qty
        if not remaining:
            break
    return DisplayedLiquidityBound(
        requested=requested,
        bounded_quantity=filled,
        unfilled_quantity=remaining,
        average_displayed_price=total_cost / filled if filled else None,
        source_sequence=book.seq_id,
    )
