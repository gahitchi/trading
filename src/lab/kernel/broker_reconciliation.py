"""Read-only broker-reported order, execution and position comparison.

No source authentication, broker access, release of reservations or order
submission. A matching report proves only consistency with reported values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from lab.kernel.order_lifecycle import LifecycleError, OfflineOrderSession, _id, _ns, _quantity

_STATES = frozenset({"open", "pending_cancel", "cancelled", "filled", "rejected", "unknown"})


@dataclass(frozen=True, slots=True)
class ReportedOrder:
    client_order_id: str
    broker_order_id: str
    status: str
    cumulative_filled: Decimal

    def __post_init__(self) -> None:
        _id(self.client_order_id, "client order")
        _id(self.broker_order_id, "broker order")
        if type(self.status) is not str or self.status not in _STATES:
            raise LifecycleError("invalid reported order status")
        _quantity(self.cumulative_filled, "broker cumulative fill")
        if self.cumulative_filled < 0:
            raise LifecycleError("negative broker cumulative fill")


@dataclass(frozen=True, slots=True)
class ReportedExecution:
    client_order_id: str
    execution_id: str
    quantity: Decimal
    exchange_ns: int
    available_ns: int

    def __post_init__(self) -> None:
        _id(self.client_order_id, "execution client order")
        _id(self.execution_id, "execution ID")
        _quantity(self.quantity, "execution quantity", positive=True)
        _ns(self.exchange_ns, "exchange")
        _ns(self.available_ns, "source availability")
        if self.exchange_ns > self.available_ns:
            raise LifecycleError("execution exchange time exceeds availability")


@dataclass(frozen=True, slots=True)
class ReportedSnapshot:
    """An unauthenticated reported snapshot, not a custody assertion."""

    account_scope: str
    instrument_id: str
    action_version: str
    snapshot_id: str
    source_id: str
    source_sha256: str
    asof_ns: int
    available_ns: int
    position: Decimal
    orders: tuple[ReportedOrder, ...]
    executions: tuple[ReportedExecution, ...]
    positions_complete: bool
    orders_complete: bool
    executions_complete: bool
    causal_boundary_complete: bool

    def __post_init__(self) -> None:
        for field in ("account_scope", "instrument_id", "action_version", "snapshot_id", "source_id"):
            _id(getattr(self, field), field)
        if type(self.source_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", self.source_sha256) is None:
            raise LifecycleError("source evidence SHA-256 is invalid")
        _ns(self.asof_ns, "as-of")
        _ns(self.available_ns, "available")
        if self.asof_ns > self.available_ns:
            raise LifecycleError("snapshot available before its as-of boundary")
        _quantity(self.position, "snapshot position")
        if type(self.orders) is not tuple or type(self.executions) is not tuple:
            raise LifecycleError("snapshot collections must be immutable")
        if any(type(order) is not ReportedOrder for order in self.orders):
            raise LifecycleError("invalid snapshot order type")
        if any(type(fill) is not ReportedExecution for fill in self.executions):
            raise LifecycleError("invalid snapshot execution type")
        if len({o.client_order_id for o in self.orders}) != len(self.orders):
            raise LifecycleError("duplicate broker client order ID")
        if len({o.broker_order_id for o in self.orders}) != len(self.orders):
            raise LifecycleError("duplicate broker order ID")
        if len({e.execution_id for e in self.executions}) != len(self.executions):
            raise LifecycleError("duplicate broker execution ID")
        if any(
            type(getattr(self, flag)) is not bool
            for flag in ("positions_complete", "orders_complete", "executions_complete", "causal_boundary_complete")
        ):
            raise LifecycleError("coverage assertions must be explicit booleans")
        for event in self.executions:
            if event.exchange_ns > self.asof_ns or event.available_ns > self.available_ns:
                raise LifecycleError("execution falls outside snapshot as-of/availability boundary")


@dataclass(frozen=True, slots=True)
class ParityAssessment:
    account_scope: str
    snapshot_id: str
    findings: tuple[str, ...]
    source_authenticated: bool = False
    broker_authorization: bool = False
    release_reservations: bool = False

    @property
    def internally_consistent(self) -> bool:
        """Reported-data equivalence only. Not a trade authorization."""
        return not self.findings


def compare_reported_snapshot(session: OfflineOrderSession, snapshot: ReportedSnapshot) -> ParityAssessment:
    """Compare the local journal with a reported broker snapshot, never mutate."""
    if type(session) is not OfflineOrderSession or type(snapshot) is not ReportedSnapshot:
        raise LifecycleError("typed offline session and snapshot required")
    issues: set[str] = set()
    if session._faulted:
        issues.add("LOCAL_JOURNAL_QUARANTINED")
    try:
        session.journal.verify()
        journal = session.journal.read_all()
        if not journal or journal[-1]["digest"] != session._head_digest:
            issues.add("LOCAL_JOURNAL_STALE")
        if journal and snapshot.asof_ns < journal[-1]["timestamp_ns"]:
            issues.add("SNAPSHOT_BEFORE_LOCAL_EVIDENCE")
    except Exception:
        issues.add("LOCAL_JOURNAL_CORRUPT")
    if snapshot.instrument_id != session.instrument_id or snapshot.action_version != session.action_version:
        issues.add("SCOPE_OR_ACTION_VERSION_MISMATCH")
    for complete, code in (
        (snapshot.positions_complete, "POSITION_COVERAGE_UNASSERTED"),
        (snapshot.orders_complete, "ORDER_COVERAGE_UNASSERTED"),
        (snapshot.executions_complete, "EXECUTION_COVERAGE_UNASSERTED"),
        (snapshot.causal_boundary_complete, "CAUSAL_FENCE_UNASSERTED"),
    ):
        if not complete:
            issues.add(code)
    if snapshot.position != session.position_from_fills:
        issues.add("POSITION_DISAGREES_WITH_LOCAL_FILL_PROJECTION")

    orders = {order.client_order_id: order for order in snapshot.orders}
    executions = {event.execution_id: event for event in snapshot.executions}
    accounted_execs: set[str] = set()
    for key, state in session.orders.items():
        remote = orders.get(key)
        if not state.prepared:
            if remote is not None:
                issues.add("UNSUBMITTED_INTENT_AT_BROKER")
            continue
        if remote is None:
            issues.add("BROKER_ORDER_MISSING_FROM_HISTORY")
            continue
        if state.broker_order_id is not None and state.broker_order_id != remote.broker_order_id:
            issues.add("BROKER_ORDER_ID_CONFLICT")
        if state.filled != remote.cumulative_filled:
            issues.add("BROKER_CUMULATIVE_FILL_MISMATCH")
        if remote.cumulative_filled > state.intent.quantity:
            issues.add("BROKER_ORDER_OVERFILL")
        if state.terminal is not None and remote.status != state.terminal:
            issues.add("BROKER_ORDER_TERMINAL_STATE_MISMATCH")
        if state.terminal is None and remote.status not in {"open", "pending_cancel", "unknown"}:
            issues.add("BROKER_TERMINAL_EVENT_NOT_JOURNALED")
        if remote.status == "unknown" or state.uncertain:
            issues.add("UNRESOLVED_BROKER_ORDER_STATE")
        if state.cancel_requested and state.terminal is None and remote.status == "open":
            issues.add("CANCEL_REQUEST_STILL_WORKING")
        amount = Decimal("0")
        for exec_id in state.executions:
            accounted_execs.add(exec_id)
            fill = executions.get(exec_id)
            if fill is None:
                issues.add("BROKER_EXECUTION_MISSING")
                continue
            if fill.client_order_id != key:
                issues.add("BROKER_EXECUTION_WRONG_ORDER")
            if fill.quantity % session.quantity_step:
                issues.add("BROKER_EXECUTION_STEP_MISMATCH")
            amount += fill.quantity
        if amount != state.filled:
            issues.add("BROKER_EXECUTION_SUM_MISMATCH")

    if set(orders) - set(session.orders):
        issues.add("EXTERNAL_OR_UNKNOWN_ORDER")
    if set(executions) - accounted_execs:
        issues.add("EXTERNAL_OR_UNKNOWN_EXECUTION")
    if session.frozen:
        issues.add("UNRESOLVED_LOCAL_FREEZE")
    return ParityAssessment(
        account_scope=snapshot.account_scope, snapshot_id=snapshot.snapshot_id, findings=tuple(sorted(issues))
    )
