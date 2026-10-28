"""F1.4: broker-neutral, non-authorizing order lifecycle and atomic economic fills.

Original external bytes are retained, but their issuer is NOT authenticated.
No network access, broker command, order creation, risk authorization or
automatic recovery capability exists in this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from lab.economic_ledger import SYMBOL, EconomicLedgerError, exact
from lab.economic_store import EconomicEventStore, EconomicStoreError, _encode
from lab.engine_events import (
    SHA256,
    BrokerFillFact,
    EngineEconomicBook,
    EngineFactError,
    _evidence_digest,
    _identity,
)
from lab.fill_input import parse_broker_fill_record
from lab.settlement_input import _reject_nonfinite, _unique_object

MAX_ORDER_EVENT_BYTES = 64 * 1024
MAX_ORDER_EVENTS = 100_000
ORDER_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "account_id",
        "record_id",
        "event_id",
        "timestamp_ns",
        "source_kind",
        "broker_order_id",
        "kind",
        "symbol",
        "side",
        "requested_quantity",
        "cumulative_executed_quantity",
        "remaining_quantity",
        "fill_record_id",
        "fill_source_sha256",
    }
)
KINDS = frozenset({"ACCEPTED", "FILL", "CANCEL_REQUESTED", "CANCELED", "REJECTED"})
GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class BrokerOrderEvent:
    event_id: str
    source_ref: str
    source_sha256: str
    timestamp_ns: int
    broker_order_id: str
    kind: str
    symbol: str
    side: str
    requested_quantity: Decimal
    cumulative_executed_quantity: Decimal
    remaining_quantity: Decimal
    fill_record_id: str | None
    fill_source_sha256: str | None


@dataclass(frozen=True, slots=True)
class BrokerOrderState:
    broker_order_id: str
    symbol: str
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    status: str
    last_timestamp_ns: int


@dataclass(frozen=True, slots=True)
class OrderLifecycleSnapshot:
    sequence: int
    states: tuple[BrokerOrderState, ...]
    evidence_head: str
    economic_head: str
    broker_source_authenticated: bool = False
    execution_authorized: bool = False

    @property
    def unresolved_order_ids(self) -> tuple[str, ...]:
        return tuple(
            state.broker_order_id
            for state in self.states
            if state.status not in {"FILLED", "CANCELED", "REJECTED"}
        )


def _quantity(value: object, description: str, *, positive: bool = False) -> Decimal:
    if type(value) is not str or len(value) > 80:
        raise EconomicStoreError(f"{description} must be a bounded decimal string")
    try:
        return exact(Decimal(value), positive=positive, nonnegative=not positive)
    except (InvalidOperation, EconomicLedgerError) as exc:
        raise EconomicStoreError(f"invalid {description}") from exc


def parse_broker_order_event(
    raw: bytes, *, source_namespace: str, expected_account_id: str
) -> BrokerOrderEvent:
    """Strictly parse a single untrusted, claimed-external order transition."""
    _identity(source_namespace, "order source namespace")
    _identity(expected_account_id, "order account")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ORDER_EVENT_BYTES:
        raise EconomicStoreError("order transition requires bounded original bytes")
    try:
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EconomicStoreError("invalid original order transition JSON") from exc
    if type(doc) is not dict or set(doc) != ORDER_EVENT_FIELDS:
        raise EconomicStoreError("incomplete or unexpected order transition fields")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise EconomicStoreError("unsupported order transition schema")
    if type(doc["account_id"]) is not str or doc["account_id"] != expected_account_id:
        raise EconomicStoreError("foreign order transition account")
    if type(doc["source_kind"]) is not str or doc["source_kind"] != "BROKER_ORDER_EVENT":
        raise EconomicStoreError("unsupported order transition source kind")
    if type(doc["timestamp_ns"]) is not int or doc["timestamp_ns"] <= 0:
        raise EconomicStoreError("order transition timestamp must be positive integer nanoseconds")
    if type(doc["kind"]) is not str or doc["kind"] not in KINDS:
        raise EconomicStoreError("unsupported broker order transition")
    if type(doc["symbol"]) is not str or not SYMBOL.fullmatch(doc["symbol"]):
        raise EconomicStoreError("invalid order instrument")
    if type(doc["side"]) is not str or doc["side"] not in ("BUY", "SELL"):
        raise EconomicStoreError("invalid order side")
    fill_id = doc["fill_record_id"]
    fill_digest = doc["fill_source_sha256"]
    if doc["kind"] == "FILL":
        _identity(fill_id, "order execution receipt identity")
        if type(fill_digest) is not str or not SHA256.fullmatch(fill_digest):
            raise EconomicStoreError("order fill requires original execution SHA-256")
    elif fill_id is not None or fill_digest is not None:
        raise EconomicStoreError("non-fill order transition cannot claim an execution")
    return BrokerOrderEvent(
        event_id=_identity(doc["event_id"], "order event identity"),
        source_ref=f"{source_namespace}:{_identity(doc['record_id'], 'order record identity')}",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        timestamp_ns=doc["timestamp_ns"],
        broker_order_id=_identity(doc["broker_order_id"], "broker order identity"),
        kind=doc["kind"],
        symbol=doc["symbol"],
        side=doc["side"],
        requested_quantity=_quantity(doc["requested_quantity"], "requested quantity", positive=True),
        cumulative_executed_quantity=_quantity(
            doc["cumulative_executed_quantity"], "cumulative executed quantity"
        ),
        remaining_quantity=_quantity(doc["remaining_quantity"], "remaining quantity"),
        fill_record_id=fill_id,
        fill_source_sha256=fill_digest,
    )


def _transition(
    old: BrokerOrderState | None, event: BrokerOrderEvent, fill: BrokerFillFact | None
) -> BrokerOrderState:
    """Replay one order event without inventing execution, volume, or cash."""
    req = event.requested_quantity
    cum = event.cumulative_executed_quantity
    rem = event.remaining_quantity
    if cum > req or rem > req:
        raise EconomicStoreError("order quantities exceed originally requested size")
    if old is None:
        if event.kind not in ("ACCEPTED", "REJECTED") or fill is not None or cum != 0:
            raise EconomicStoreError("order lacks its initial broker acknowledgment or rejection")
        if event.kind == "ACCEPTED" and rem != req:
            raise EconomicStoreError("accepted order is missing its full initial remainder")
        if event.kind == "REJECTED" and rem != 0:
            raise EconomicStoreError("rejected order cannot retain open quantity")
        return BrokerOrderState(
            event.broker_order_id, event.symbol, event.side,
            req, Decimal(0), event.kind, event.timestamp_ns,
        )
    if (
        old.status in ("FILLED", "CANCELED", "REJECTED")
        or event.timestamp_ns < old.last_timestamp_ns
        or event.symbol != old.symbol
        or event.side != old.side
        or event.broker_order_id != old.broker_order_id
        or req != old.requested_quantity
    ):
        raise EconomicStoreError("terminal, reordered or conflicting broker order transition")
    status = old.status
    if event.kind == "FILL":
        if fill is None or fill.quantity <= 0:
            raise EconomicStoreError("order execution lacks a classified fill")
        if (
            fill.broker_order_id != event.broker_order_id
            or fill.symbol != event.symbol
            or fill.side != event.side
            or fill.timestamp_ns != event.timestamp_ns
        ):
            raise EconomicStoreError("order execution and financial fill disagree")
        expected_cum = old.filled_quantity + fill.quantity
        if cum != expected_cum or cum > req or rem != req - cum:
            raise EconomicStoreError("broker fill is nonconserving, duplicated or overfilled")
        status = "FILLED" if cum == req else "CANCEL_PENDING" if status == "CANCEL_PENDING" else "PARTIALLY_FILLED"
    elif event.kind == "CANCEL_REQUESTED":
        if status == "CANCEL_PENDING":
            raise EconomicStoreError("duplicate cancel request must reuse its original event identity")
        if fill is not None or cum != old.filled_quantity or rem != req - cum:
            raise EconomicStoreError("cancel request changed settled executed quantity")
        status = "CANCEL_PENDING"
    elif event.kind == "CANCELED":
        if fill is not None or cum != old.filled_quantity or rem != 0:
            raise EconomicStoreError("cancel confirmation invents or conceals a fill")
        status = "CANCELED"
    else:
        raise EconomicStoreError("new acceptance or rejection cannot replace an existing order")
    return BrokerOrderState(
        event.broker_order_id, event.symbol, event.side,
        req, cum, status, event.timestamp_ns,
    )


def _chain(previous: str, event: BrokerOrderEvent, pre: str, post: str) -> str:
    raw = json.dumps(
        {
            "schema": 1,
            "previous": previous,
            "order_source_ref": event.source_ref,
            "order_source_sha256": event.source_sha256,
            "fill_record_id": event.fill_record_id,
            "fill_sha256": event.fill_source_sha256,
            "pre_economic_head": pre,
            "post_economic_head": post,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _economic_heads(book: EngineEconomicBook) -> list[str]:
    head = GENESIS
    result = [head]
    for fact in book.events:
        head = _evidence_digest(head, book.account_id, fact)
        result.append(head)
    if head != book.evidence_head:
        raise EconomicStoreError("economic journal evidence head is inconsistent")
    return result


def _replay(db: sqlite3.Connection, book: EngineEconomicBook) -> OrderLifecycleSnapshot:
    heads = _economic_heads(book)
    locations = {head: index for index, head in enumerate(heads)}
    states: dict[str, BrokerOrderState] = {}
    seen_events: set[str] = set()
    seen_fills: set[str] = set()
    last_after = 0
    chain = GENESIS
    rows = db.execute(
        "SELECT seq,event_id,source_ref,broker_order_id,source_namespace,source_sha256,"
        "source_bytes,fill_source_bytes,pre_economic_head,post_economic_head,order_chain "
        "FROM broker_order_events ORDER BY seq"
    )
    count = 0
    for row in rows:
        count += 1
        (
            seq, eid, sref, order_id, namespace, digest, raw_order, raw_fill,
            pre, post, observed_chain,
        ) = row
        if count > MAX_ORDER_EVENTS or seq != count or type(raw_order) is not bytes:
            raise EconomicStoreError("broker order journal sequence or original bytes are corrupted")
        event = parse_broker_order_event(
            raw_order, source_namespace=namespace, expected_account_id=book.account_id
        )
        if (
            event.event_id != eid
            or event.source_ref != sref
            or event.broker_order_id != order_id
            or event.source_sha256 != digest
            or eid in seen_events
            or pre not in locations
            or post not in locations
        ):
            raise EconomicStoreError("order event identity, custody or economic fence is corrupted")
        before = locations[pre]
        after = locations[post]
        if before < last_after:
            raise EconomicStoreError("broker order events regress the economic ledger chronology")
        fill = None
        if event.kind == "FILL":
            if after != before + 1 or type(raw_fill) is not bytes:
                raise EconomicStoreError("order fill is not atomic with exactly one economic event")
            fill = parse_broker_fill_record(
                raw_fill, source_namespace=namespace, expected_account_id=book.account_id
            )
            expected_ref = f"{namespace}:{event.fill_record_id}"
            if (
                fill.source_ref != expected_ref
                or fill.source_sha256 != event.fill_source_sha256
                or fill.source_ref in seen_fills
                or book.events[before] != fill
            ):
                raise EconomicStoreError("broker execution source and settled economic event diverged")
            seen_fills.add(fill.source_ref)
        elif after != before or raw_fill is not None:
            raise EconomicStoreError("non-fill order state cannot change economic source head")
        states[event.broker_order_id] = _transition(states.get(event.broker_order_id), event, fill)
        new_chain = _chain(chain, event, pre, post)
        if observed_chain != new_chain:
            raise EconomicStoreError("order source digest chain does not match original bytes")
        chain = new_chain
        seen_events.add(eid)
        last_after = after
    tracked = set(states)
    for fact in book.events:
        if type(fact) is BrokerFillFact and fact.broker_order_id in tracked and fact.source_ref not in seen_fills:
            raise EconomicStoreError("unlinked execution for an order under lifecycle custody")
    return OrderLifecycleSnapshot(
        count,
        tuple(states[key] for key in sorted(states)),
        chain,
        book.evidence_head,
    )


class OrderLifecycleStore:
    """Transactional F1.4 observation archive, never an order-routing engine."""

    def __init__(self, economic_store: EconomicEventStore):
        if type(economic_store) is not EconomicEventStore:
            raise EconomicStoreError("order lifecycle requires an independent economic journal")
        self.store = economic_store

    def inspect(self) -> OrderLifecycleSnapshot:
        with self.store._connect() as db:
            db.execute("BEGIN")
            book = self.store._read_locked(db)
            result = _replay(db, book)
            db.execute("COMMIT")
            return result

    def append(
        self,
        raw_order: bytes,
        *,
        source_namespace: str,
        expected_head: str,
        raw_fill: bytes | None = None,
    ) -> OrderLifecycleSnapshot:
        """Atomic order evidence and fill ledger commit, with exact-retry safety.

        Uncertain crash outcomes require inspecting the journal and retrying
        the same original evidence; no order is sent or automatically resumed.
        """
        if self.store.readonly:
            raise EconomicStoreError("read-only order lifecycle cannot append")
        if type(expected_head) is not str or not SHA256.fullmatch(expected_head):
            raise EconomicStoreError("order transition requires exact economic-head fence")
        event = parse_broker_order_event(
            raw_order,
            source_namespace=source_namespace,
            expected_account_id=self.store.account_id,
        )
        if event.kind != "FILL" and raw_fill is not None:
            raise EconomicStoreError("non-fill order event cannot carry execution bytes")
        fill = None
        if event.kind == "FILL":
            if type(raw_fill) is not bytes:
                raise EconomicStoreError("fill transition requires original broker execution bytes")
            fill = parse_broker_fill_record(
                raw_fill, source_namespace=source_namespace, expected_account_id=self.store.account_id
            )
            if (
                fill.source_ref != f"{source_namespace}:{event.fill_record_id}"
                or fill.source_sha256 != event.fill_source_sha256
            ):
                raise EconomicStoreError("execution bytes do not match the order transition")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            book = self.store._read_locked(db)
            snapshot = _replay(db, book)
            existing = db.execute(
                "SELECT source_ref,source_bytes,fill_source_bytes,pre_economic_head "
                "FROM broker_order_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                if existing != (event.source_ref, raw_order, raw_fill, expected_head):
                    raise EconomicStoreError("reused order event identity disagrees with committed evidence")
                db.execute("COMMIT")
                return snapshot
            if expected_head != book.evidence_head:
                raise EconomicStoreError("stale economic head during order transition")
            if snapshot.sequence >= MAX_ORDER_EVENTS:
                raise EconomicStoreError("bounded order transition journal is full")
            states = {state.broker_order_id: state for state in snapshot.states}
            _transition(states.get(event.broker_order_id), event, fill)
            prior_head = book.evidence_head
            if fill is not None:
                book = book.append(fill)
                payload = _encode(fill)
                db.execute(
                    "INSERT INTO economic_events "
                    "(seq,event_id,source_ref,payload,source_bytes,evidence_head,ledger_head) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        len(book.events),
                        fill.event_id,
                        fill.source_ref,
                        payload,
                        raw_fill,
                        book.evidence_head,
                        book.ledger.journal[-1].sha256,
                    ),
                )
            next_chain = _chain(snapshot.evidence_head, event, prior_head, book.evidence_head)
            db.execute(
                "INSERT INTO broker_order_events "
                "(seq,event_id,source_ref,broker_order_id,source_namespace,source_sha256,"
                "source_bytes,fill_source_bytes,pre_economic_head,post_economic_head,order_chain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot.sequence + 1,
                    event.event_id,
                    event.source_ref,
                    event.broker_order_id,
                    source_namespace,
                    event.source_sha256,
                    raw_order,
                    raw_fill,
                    prior_head,
                    book.evidence_head,
                    next_chain,
                ),
            )
            verified = _replay(db, book)
            db.execute("COMMIT")
            return verified
