"""Portable, offline-only order state reducer and durable replay.

Models only risk-reducing order intents with an already asserted broker snapshot.
No execution adapter, network transport, account authentication, or broker
permission is provided. A prepared dispatch is UNKNOWN until broker evidence
is reconciled; a cancel request never frees inventory on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal

from lab.markets.domain import OrderIntent, Side
from lab.markets.journal import EventJournal

_NS_MAX = 2**63 - 1
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,128}\Z")


class LifecycleError(ValueError):
    """Broker/order evidence is missing, conflicting or unsafe for automation."""


def _id(value: str, label: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise LifecycleError(f"invalid {label}")
    return value


def _ns(value: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _NS_MAX:
        raise LifecycleError(f"invalid {label} time")
    return value


def _quantity(value: Decimal, label: str, *, positive: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or (positive and value <= 0):
        raise LifecycleError(f"invalid exact Decimal {label}")
    return value


def _digest(record: dict) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class BrokerEvidence:
    """Untrusted until verified by a future authenticated broker adapter."""

    event_id: str
    source_id: str
    source_sha256: str
    exchange_ns: int
    available_ns: int
    action_version: str

    def __post_init__(self) -> None:
        _id(self.event_id, "source event ID")
        _id(self.source_id, "source identity")
        if type(self.source_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", self.source_sha256):
            raise LifecycleError("source evidence SHA-256 required")
        _ns(self.exchange_ns, "exchange")
        _ns(self.available_ns, "source availability")
        if self.available_ns < self.exchange_ns:
            raise LifecycleError("source event not yet available")
        _id(self.action_version, "instrument action version")

    def record(self) -> dict:
        return {
            "event_id": self.event_id,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "exchange_ns": self.exchange_ns,
            "available_ns": self.available_ns,
            "action_version": self.action_version,
        }


@dataclass(frozen=True, slots=True)
class OrderState:
    intent: OrderIntent
    action_version: str
    prepared: bool = False
    broker_order_id: str | None = None
    filled: Decimal = Decimal("0")
    cancel_requested: bool = False
    terminal: str | None = None
    uncertain: bool = False
    executions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _id(self.action_version, "action version")
        _quantity(self.filled, "filled quantity")
        if self.filled < 0 or self.filled > self.intent.quantity:
            raise LifecycleError("filled quantity violates original order")
        if self.terminal not in (None, "cancelled", "rejected", "filled"):
            raise LifecycleError("unrecognized terminal state")
        if self.broker_order_id is not None:
            _id(self.broker_order_id, "broker order ID")
        if type(self.executions) is not tuple or len(set(self.executions)) != len(self.executions):
            raise LifecycleError("duplicate or mutable execution IDs")
        for item in self.executions:
            _id(item, "execution ID")
        if self.terminal == "filled" and self.filled != self.intent.quantity:
            raise LifecycleError("filled order must account for entire original quantity")
        if self.terminal == "rejected" and self.filled:
            raise LifecycleError("partial fill cannot become full order rejection")
        if any(type(flag) is not bool for flag in (self.prepared, self.cancel_requested, self.uncertain)):
            raise LifecycleError("lifecycle flags require explicit booleans")

    @property
    def remaining(self) -> Decimal:
        return self.intent.quantity - self.filled

    @property
    def reserved(self) -> Decimal:
        # Cancellation-in-flight, unresolved dispatch and post-cancel late
        # fill all reserve the remaining quantity until broker reconciliation.
        return self.remaining if self.terminal is None or self.uncertain else Decimal("0")

    def record(self) -> dict:
        return {
            "intent": {
                "client_order_id": self.intent.client_order_id,
                "instrument_id": self.intent.instrument_id,
                "side": self.intent.side.value,
                "quantity": str(self.intent.quantity),
                "created_ns": self.intent.created_ns,
                "reduce_only": self.intent.reduce_only,
            },
            "action_version": self.action_version,
            "prepared": self.prepared,
            "broker_order_id": self.broker_order_id,
            "filled": str(self.filled),
            "cancel_requested": self.cancel_requested,
            "terminal": self.terminal,
            "uncertain": self.uncertain,
            "executions": list(self.executions),
        }


class OfflineOrderSession:
    """A single-instrument reducer, persisted *before* acknowledging events.

    Future integration requires atomic broker snapshots and scoped client IDs.
    An externally supplied source hash is provenance metadata, not proof that
    a broker actually issued the event. No class method sends orders.
    """

    def __init__(
        self, journal: EventJournal, *, instrument_id: str, action_version: str,
        starting_position: Decimal, quantity_step: Decimal
    ) -> None:
        if type(journal) is not EventJournal:
            raise LifecycleError("verified EventJournal required")
        self.journal = journal
        self.instrument_id = _id(instrument_id, "instrument")
        self.action_version = _id(action_version, "instrument version")
        self.starting_position = _quantity(starting_position, "opening position")
        self.quantity_step = _quantity(quantity_step, "quantity step", positive=True)
        self.orders: dict[str, OrderState] = {}
        self.seen_events: dict[str, dict] = {}
        self._faulted = False
        journal.verify()
        header = {
            "schema": 1,
            "mode": "OFFLINE_ONLY",
            "instrument_id": self.instrument_id,
            "action_version": self.action_version,
            "starting_position": str(self.starting_position),
            "quantity_step": str(self.quantity_step),
        }
        events = journal.read_all()
        if not events:
            journal.append("session:independent-order:v1", "session", 0, header)
        else:
            first = events[0]
            if first["event_id"] != "session:independent-order:v1" or first["kind"] != "session" or first["payload"] != header or first["timestamp_ns"] != 0:
                raise LifecycleError("opening broker-state assumptions differ or journal uses an incompatible stream")
        self.recover()

    def state_digest(self) -> str:
        return _digest({
            "orders": [self.orders[key].record() for key in sorted(self.orders)],
            "position_from_fills": str(self.position_from_fills),
        })

    @property
    def position_from_fills(self) -> Decimal:
        """UNAUTHENTICATED local projection, never brokerage position."""
        return self.starting_position + sum(
            (order.filled * (Decimal(1) if order.intent.side is Side.BUY else Decimal(-1))
             for order in self.orders.values()),
            Decimal(0),
        )

    @property
    def reserved_reduction(self) -> Decimal:
        return sum((order.reserved for order in self.orders.values()), Decimal(0))

    @property
    def frozen(self) -> bool:
        return any(order.uncertain for order in self.orders.values())

    def _apply(self, kind: str, data: dict) -> None:
        oid = data["order_id"]
        if kind == "intent":
            if oid in self.orders:
                raise LifecycleError("duplicate order intent")
            intent_data = data["intent"]
            intent = OrderIntent(
                client_order_id=intent_data["client_order_id"],
                instrument_id=intent_data["instrument_id"],
                side=Side(intent_data["side"]),
                quantity=Decimal(intent_data["quantity"]),
                created_ns=intent_data["created_ns"],
                reduce_only=intent_data["reduce_only"],
            )
            if not intent.reduce_only or intent.instrument_id != self.instrument_id or data["action_version"] != self.action_version:
                raise LifecycleError("only same-version reduce-only reference orders are supported")
            if intent.quantity % self.quantity_step:
                raise LifecycleError("requested quantity violates current instrument lot step")
            if self.frozen:
                raise LifecycleError("unreconciled source evidence freezes new intents")
            signed = Decimal(-1) if intent.side is Side.SELL else Decimal(1)
            held = self.position_from_fills
            if not held or signed * held >= 0 or self.reserved_reduction + intent.quantity > abs(held):
                raise LifecycleError("reduce-only reservation could reverse or exceed inventory")
            self.orders[oid] = OrderState(intent=intent, action_version=self.action_version)
            return
        if oid not in self.orders:
            raise LifecycleError("unknown order; no executable orphan fill")
        original = self.orders[oid]
        state = original
        if kind == "submitted":
            if self.frozen:
                raise LifecycleError("unreconciled source state freezes new submission attempts")
            if state.prepared or state.terminal:
                raise LifecycleError("duplicate/terminal submission attempt")
            state = replace(state, prepared=True)
        elif kind == "ack":
            if not state.prepared or state.broker_order_id is not None or state.terminal:
                raise LifecycleError("unexpected broker acknowledgement")
            broker_id = _id(data["broker_order_id"], "broker order")
            if any(x.broker_order_id == broker_id for x in self.orders.values()):
                raise LifecycleError("broker order identity collision")
            state = replace(state, broker_order_id=broker_id)
        elif kind == "fill":
            if not state.prepared or state.terminal == "rejected":
                raise LifecycleError("fill without outstanding broker submission")
            qty = Decimal(data["quantity"])
            _quantity(qty, "broker execution quantity", positive=True)
            exec_id = _id(data["execution_id"], "execution ID")
            if any(exec_id in s.executions for s in self.orders.values()):
                raise LifecycleError("execution ID replay under a different source event/order")
            if qty > state.remaining or qty % self.quantity_step:
                raise LifecycleError("overfill or invalid venue step")
            late = state.terminal == "cancelled"
            state = replace(
                state, filled=state.filled + qty,
                terminal="cancelled" if late else ("filled" if state.filled + qty == state.intent.quantity else None),
                uncertain=state.uncertain or late,
                executions=(*state.executions, exec_id),
            )
        elif kind == "cancel":
            if data["phase"] == "requested":
                if not state.prepared or state.cancel_requested or state.terminal:
                    raise LifecycleError("invalid duplicate or terminal cancellation request")
                state = replace(state, cancel_requested=True)
            elif data["phase"] == "confirmed":
                if not state.cancel_requested or state.terminal:
                    raise LifecycleError("cancel confirmation must correspond to unresolved cancellation")
                # Even a 'cancelled' status is not a complete ordered broker
                # snapshot. Late fills and other GTC reservations may race.
                # The v1 kernel has NO verified reconciliation release gate.
                state = replace(state, terminal="cancelled", uncertain=True)
            else:
                raise LifecycleError("unrecognized cancel phase")
        elif kind == "reject":
            if not state.prepared or state.terminal or state.filled:
                raise LifecycleError("cannot reject an already executed order")
            state = replace(state, terminal="rejected")
        elif kind == "unknown":
            if not state.prepared or state.terminal:
                raise LifecycleError("unconfirmed broker state must originate from prepared attempt")
            state = replace(state, uncertain=True)
        else:
            raise LifecycleError("unrecognized order lifecycle transition")
        self.orders[oid] = state

    def _verify_event_record(self, kind: str, event_id: str, timestamp_ns: int, data: dict) -> None:
        """Bind durable IDs, source availability and event shape during replay."""
        oid = _id(data.get("order_id"), "journal order")
        if kind == "intent":
            if set(data) != {"order_id", "intent", "action_version"} or event_id != "plan:" + oid:
                raise LifecycleError("noncanonical order planning event")
            if type(data["intent"]) is not dict or data["intent"].get("client_order_id") != oid:
                raise LifecycleError("unbound original client intent")
            if timestamp_ns != data["intent"].get("created_ns"):
                raise LifecycleError("intent timestamp not bound to original intent")
        elif kind == "submitted":
            if set(data) != {"order_id", "transport"} or data["transport"] != "offline_no_network":
                raise LifecycleError("dispatch must remain offline only")
            if event_id != "prepare:" + oid:
                raise LifecycleError("dispatch event ID not bound to intent")
        elif kind == "cancel" and data.get("phase") == "requested":
            if set(data) != {"order_id", "phase"} or event_id != "cancel-request:" + oid:
                raise LifecycleError("unbound cancellation request")
        else:
            expected_kind = "cancel" if kind == "cancel" else kind
            fields = {
                "ack": {"order_id", "source", "broker_order_id"},
                "fill": {"order_id", "source", "quantity", "execution_id"},
                "cancel": {"order_id", "source", "phase"},
                "reject": {"order_id", "source"},
                "unknown": {"order_id", "source"},
            }
            if expected_kind not in fields or set(data) != fields[expected_kind]:
                raise LifecycleError("unexpected source event fields")
            if kind == "cancel" and data["phase"] != "confirmed":
                raise LifecycleError("unexpected source cancellation phase")
            source = data["source"]
            if type(source) is not dict or set(source) != set(BrokerEvidence.__dataclass_fields__):
                raise LifecycleError("broker source evidence missing fields")
            broker = BrokerEvidence(**source)
            if broker.action_version != self.action_version or timestamp_ns != broker.available_ns:
                raise LifecycleError("source event is stale or incompatible with instrument")
            if event_id != "source:" + _digest({"order_id": oid, "event_id": broker.event_id}):
                raise LifecycleError("source event identity mismatch")
            if kind == "fill" and type(data["quantity"]) is not str:
                raise LifecycleError("fill must be an exact decimal string")
        _ns(timestamp_ns, "journal observed")

    def _post(self, kind: str, *, order_id: str, timestamp_ns: int, data: dict, event_id: str) -> OrderState:
        _id(order_id, "client order ID")
        _id(event_id, "event identity")
        _ns(timestamp_ns, "observed")
        if self._faulted:
            raise LifecycleError("order session is quarantined after invalid journal replay")
        full = {"order_id": order_id, **data}
        signature = {"kind": kind, "timestamp_ns": timestamp_ns, "data": full}
        if event_id in self.seen_events:
            if self.seen_events[event_id] != signature:
                raise LifecycleError("conflicting event replay under an existing ID")
            return self.orders[order_id]
        if timestamp_ns < self.journal.read_all()[-1]["timestamp_ns"]:
            raise LifecycleError("new journal observation regresses behind prior source availability")
        self._verify_event_record(kind, event_id, timestamp_ns, full)
        previous_orders = self.orders.copy()
        try:
            self._apply(kind, full)
            after_digest = self.state_digest()
            self.journal.append(event_id, kind, timestamp_ns, {"event": full, "state_sha256": after_digest})
        except Exception:
            self.orders = previous_orders
            raise
        self.seen_events[event_id] = signature
        return self.orders[order_id]

    def plan(self, intent: OrderIntent, *, action_version: str) -> OrderState:
        if type(intent) is not OrderIntent:
            raise LifecycleError("typed offline intent required")
        data = {
            "intent": {
                "client_order_id": intent.client_order_id,
                "instrument_id": intent.instrument_id,
                "side": intent.side.value,
                "quantity": str(intent.quantity),
                "created_ns": intent.created_ns,
                "reduce_only": intent.reduce_only,
            },
            "action_version": _id(action_version, "action version"),
        }
        return self._post(
            "intent", order_id=intent.client_order_id, timestamp_ns=intent.created_ns,
            data=data, event_id="plan:" + intent.client_order_id
        )

    def prepare_dispatch(self, order_id: str, *, timestamp_ns: int) -> OrderState:
        """Durable intent boundary; does NOT dispatch to a real broker."""
        return self._post(
            "submitted", order_id=order_id, timestamp_ns=timestamp_ns,
            data={"transport": "offline_no_network"}, event_id="prepare:" + order_id
        )

    def request_cancel(self, order_id: str, *, timestamp_ns: int) -> OrderState:
        """Only records a cancel request; the full remainder remains reserved."""
        return self._post(
            "cancel", order_id=order_id, timestamp_ns=timestamp_ns,
            data={"phase": "requested"}, event_id="cancel-request:" + order_id
        )

    def observe(
        self, order_id: str, kind: str, evidence: BrokerEvidence, *,
        broker_order_id: str | None = None, quantity: Decimal | None = None,
        execution_id: str | None = None,
    ) -> OrderState:
        if kind not in {"ack", "fill", "cancel_confirmed", "reject", "unknown"}:
            raise LifecycleError("unsupported external evidence event")
        if type(evidence) is not BrokerEvidence:
            raise LifecycleError("typed source evidence required")
        if evidence.action_version != self.action_version:
            raise LifecycleError("instrument definition or split version mismatch; freeze and reconcile")
        if order_id not in self.orders or evidence.available_ns < self.orders[order_id].intent.created_ns:
            raise LifecycleError("unknown order or future knowledge")
        data = {"source": evidence.record()}
        event_kind = "cancel" if kind == "cancel_confirmed" else kind
        if kind == "ack":
            data["broker_order_id"] = _id(broker_order_id, "broker order ID")
        elif kind == "fill":
            qty = _quantity(quantity, "fill quantity", positive=True)
            data["quantity"] = str(qty)
            data["execution_id"] = _id(execution_id, "execution ID")
        elif kind == "cancel_confirmed":
            data["phase"] = "confirmed"
        return self._post(
            event_kind, order_id=order_id, timestamp_ns=evidence.available_ns, data=data,
            event_id="source:" + _digest({"order_id": order_id, "event_id": evidence.event_id})
        )

    def recover(self) -> None:
        """Invalid replay quarantines this instance pending a freshly verified session."""
        self._faulted = True
        self.journal.verify()
        events = self.journal.read_all()
        self.orders = {}
        self.seen_events = {}
        previous_ts = 0
        for entry in events[1:]:
            if entry["timestamp_ns"] < previous_ts:
                raise LifecycleError("event availability timestamp regresses")
            previous_ts = entry["timestamp_ns"]
            if entry["kind"] not in {"intent", "submitted", "ack", "fill", "cancel", "reject", "unknown"}:
                raise LifecycleError("foreign journal event in offline order lifecycle")
            payload = entry["payload"]
            if type(payload) is not dict or set(payload) != {"event", "state_sha256"}:
                raise LifecycleError("noncanonical order event")
            data = payload["event"]
            if type(data) is not dict or "order_id" not in data:
                raise LifecycleError("missing order identity")
            if entry["event_id"] in self.seen_events:
                raise LifecycleError("duplicate journal event ID")
            self._verify_event_record(entry["kind"], entry["event_id"], entry["timestamp_ns"], data)
            self._apply(entry["kind"], data)
            if payload["state_sha256"] != self.state_digest():
                raise LifecycleError("deterministic order-state replay diverged")
            self.seen_events[entry["event_id"]] = {
                "kind": entry["kind"], "timestamp_ns": entry["timestamp_ns"], "data": data
            }
        self._faulted = False

    def assert_recovered(self) -> None:
        remembered = self.state_digest()
        self.recover()
        if self.state_digest() != remembered:
            self._faulted = True
            raise LifecycleError("in-memory order state differs from durable events")
