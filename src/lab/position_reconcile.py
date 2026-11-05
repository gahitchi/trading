"""Causal simulated-fill -> daily-equity bridge, not a broker or order gateway.

Uses full, independently sourced position-price snapshots. For each fill
a realized trade record and post-fill aggregate unrealized mark are validated
and committed as one journal batch. A complete, append-only replay source
must be supplied on restart; old source/journal divergence stops replay.
No API keys, exchange orders, or unqualified live execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from lab.profit_allocation import AllocationEvent, AllocationPolicy, ProfitAllocationLedger
from lab.profit_journal import ProfitJournal

_SYMBOL = re.compile(r"[A-Z][A-Z0-9_-]{1,39}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,110}\Z")
_ZERO = Decimal(0)
MAX_MARK_AGE = timedelta(seconds=5)


def _exact(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise ValueError(f"{name} requires an exact decimal string")
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not decimal") from exc
    if not d.is_finite() or (positive and d <= 0) or (nonnegative and d < 0):
        raise ValueError(f"{name} is nonfinite or outside allowed range")
    return d


def _check_time(ts):
    if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        raise ValueError("source timestamp must be UTC aware")


@dataclass(frozen=True)
class MarkQuote:
    price: str
    observed_at: datetime
    source_id: str


@dataclass(frozen=True)
class SimulatedFill:
    source_id: str
    timestamp: datetime
    symbol: str
    side: str
    quantity: str
    fill_price: str
    fee: str
    additional_slippage: str
    marks: dict[str, MarkQuote]  # complete independently timestamped source observations


@dataclass(frozen=True)
class PositionMark:
    source_id: str
    timestamp: datetime
    marks: dict[str, MarkQuote]
    close_day: bool = False


@dataclass(frozen=True)
class CashAdjustment:
    source_id: str
    timestamp: datetime
    amount: str
    reason: str  # fee/funding/financing/fx/correction


@dataclass(frozen=True)
class Position:
    quantity: Decimal  # signed, BUY positive and SELL negative
    average_price: Decimal


def _source_digest(row: SimulatedFill | PositionMark | CashAdjustment) -> str:
    """Bind economic journal IDs to the *complete* raw source record.

    A reissued fill with changed fee, quantity or quote provenance must
    not look like the same immutable observation, even if reported
    aggregate P&L coincidentally stays identical.
    """
    def primitive(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: primitive(v) for key, v in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [primitive(v) for v in value]
        if hasattr(value, "__dataclass_fields__"):
            return primitive(asdict(value))
        return value

    encoded = json.dumps({
        "source_type": type(row).__name__,
        "record": primitive(row),
    }, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mark_total(
    positions: dict[str, Position],
    marks: dict[str, MarkQuote],
    quantum: Decimal,
    timestamp: datetime,
) -> Decimal:
    required = {sym for sym, position in positions.items() if position.quantity}
    if not isinstance(marks, dict) or set(marks) != required:
        raise ValueError("position marks must cover exactly all open symbols; missing marks are not zero")
    amount = _ZERO
    for symbol in sorted(required):
        quote = marks[symbol]
        if not isinstance(quote, MarkQuote):
            raise ValueError("position valuation requires sourced timestamped MarkQuote")
        _check_time(quote.observed_at)
        if not isinstance(quote.source_id, str) or not _ID.fullmatch(quote.source_id):
            raise ValueError("invalid mark observation source identity")
        if quote.observed_at > timestamp:
            raise ValueError("future-dated mark observation is prohibited")
        if timestamp - quote.observed_at > MAX_MARK_AGE:
            raise ValueError("stale position mark exceeds the five-second causal tolerance")
        px = _exact(quote.price, f"{symbol} mark price", positive=True)
        pos = positions[symbol]
        amount += (pos.quantity * (px - pos.average_price)).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return amount


def _next_position(old: Position | None, side: str, qty: Decimal, price: Decimal, quantum: Decimal):
    current = old if old is not None else Position(_ZERO, _ZERO)
    signed_fill = qty if side == "BUY" else -qty
    opened = current.quantity
    realized = _ZERO
    if opened and signed_fill * opened < 0:
        closing = min(abs(opened), qty)
        direction = Decimal(1) if opened > 0 else Decimal(-1)
        realized = (direction * closing * (price - current.average_price)).quantize(
            quantum, rounding=ROUND_HALF_EVEN
        )
    remaining = opened + signed_fill
    if remaining == 0:
        return None, realized
    if not opened or (opened * signed_fill < 0 and abs(signed_fill) > abs(opened)):
        average = price
    elif opened * signed_fill < 0:
        average = current.average_price
    else:
        average = (
            abs(opened) * current.average_price + abs(signed_fill) * price
        ) / abs(remaining)
    return Position(remaining, average), realized


def compile_source(
    policy: AllocationPolicy, start_day: date,
    records: list[SimulatedFill | PositionMark | CashAdjustment],
) -> tuple[list[list[AllocationEvent]], dict[str, Position]]:
    """Compile a frozen, chronologically ordered simulated market-event source.

    All prices and quantities describe *linear* contracts in the policy's
    account currency, with multiplier=1. No inverse contracts or FX.
    Fees/additional slippage are monetary charges; if the fill price
    already includes execution slippage, set additional_slippage='0'.
    Financial source authentication and real venue matching are out of scope.
    """
    if not isinstance(records, list):
        raise ValueError("source records must be an ordered list")
    q = _exact(policy.quantum, "money quantum", positive=True)
    ledger = ProfitAllocationLedger(policy, start_day)
    positions: dict[str, Position] = {}
    batches: list[list[AllocationEvent]] = []
    seen: set[str] = set()
    last_time: datetime | None = None
    for row in records:
        if not isinstance(row, (SimulatedFill, PositionMark, CashAdjustment)):
            raise ValueError("unrecognized source event")
        _check_time(row.timestamp)
        if not isinstance(row.source_id, str) or not _ID.fullmatch(row.source_id) or row.source_id in seen:
            raise ValueError("invalid or duplicate source event ID")
        if last_time is not None and row.timestamp < last_time:
            raise ValueError("source events are out of chronological order")
        seen.add(row.source_id)
        last_time = row.timestamp
        fingerprint = _source_digest(row)
        if isinstance(row, SimulatedFill):
            if not _SYMBOL.fullmatch(row.symbol) or row.side not in {"BUY", "SELL"}:
                raise ValueError("invalid linear-contract symbol or fill side")
            qty = _exact(row.quantity, "fill quantity", positive=True)
            px = _exact(row.fill_price, "fill price", positive=True)
            fee = _exact(row.fee, "fee", nonnegative=True)
            slip = _exact(row.additional_slippage, "additional slippage", nonnegative=True)
            if fee != fee.quantize(q) or slip != slip.quantize(q):
                raise ValueError("execution costs must use account currency quantum")
            new_pos, gross = _next_position(positions.get(row.symbol), row.side, qty, px, q)
            positions = positions.copy()
            if new_pos is None:
                positions.pop(row.symbol, None)
            else:
                positions[row.symbol] = new_pos
            mark_value = _mark_total(positions, row.marks, q, row.timestamp)
            net = gross - fee - slip
            batch = [
                AllocationEvent(
                    f"e.{fingerprint}.fill", f"{row.source_id}.fill", row.timestamp,
                    policy.currency, "trade", str(net), gross=str(gross),
                    fees=str(fee), slippage=str(slip), funding="0",
                    released_mark="0",
                ),
                AllocationEvent(
                    f"e.{fingerprint}.mark", f"{row.source_id}.mark", row.timestamp,
                    policy.currency, "mark", str(mark_value),
                ),
            ]
        elif isinstance(row, PositionMark):
            amount = _mark_total(positions, row.marks, q, row.timestamp)
            batch = [AllocationEvent(
                f"e.{fingerprint}.mark", f"{row.source_id}.mark", row.timestamp,
                policy.currency, "day_close" if row.close_day else "mark", str(amount),
            )]
        else:
            amount = _exact(row.amount, "cash adjustment")
            if amount != amount.quantize(q):
                raise ValueError("cash settlement must be quantized")
            batch = [AllocationEvent(
                f"e.{fingerprint}.settle", f"{row.source_id}.settle", row.timestamp,
                policy.currency, "adjustment", str(amount), reason=row.reason,
            )]
        for event in batch:
            ledger.apply(event)  # strategy-free, fail-closed economic validation
        batches.append(batch)
    return batches, positions


def reconcile_to_journal(
    journal: ProfitJournal,
    records: list[SimulatedFill | PositionMark | CashAdjustment],
) -> dict[str, Position]:
    """Reconcile a complete immutable source stream with persisted economic events.

    Prefix must match exactly: no missing records, changed fills/marks or
    accidental attempts to replace older history. New records are committed
    in coherent batches. This caller must be the only bridge writer and
    must supply the entire immutable source history on each invocation;
    an external source archive or data connector is not implemented here.
    """
    batches, positions = compile_source(journal.policy, journal.start_day, records)
    current = list(journal.events())
    prefix_len = 0
    matched = 0
    for batch in batches:
        if prefix_len >= len(current):
            break
        end = prefix_len + len(batch)
        if current[prefix_len:end] != batch:
            raise ValueError("source/journal economic event mismatch or incomplete fill batch")
        prefix_len = end
        matched += 1
    if prefix_len != len(current):
        raise ValueError("journal contains events outside supplied source history")
    for batch in batches[matched:]:
        journal.append_batch(batch)
    return positions
