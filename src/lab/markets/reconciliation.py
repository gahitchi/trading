"""Independent, fail-closed demo-account reconciliation *decision* oracle.

Does not connect, cancel, submit, amend or adopt orders. The producing adapter
must separately prove authenticated account, region and demo environment.
Matching this snapshot cannot alone qualify an external execution adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, Side, TradingMode, decimal


class OrderState(StrEnum):
    WORKING = "working"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def open_or_uncertain(self) -> bool:
        return self in (OrderState.WORKING, OrderState.PARTIAL, OrderState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class RecordedOrder:
    client_id: str
    instrument_id: str
    side: Side
    original: Decimal
    filled: Decimal
    status: OrderState

    def __post_init__(self) -> None:
        if not re.fullmatch("[A-Za-z0-9]{1,32}", self.client_id):
            raise ValueError("invalid client order ID")
        if not self.instrument_id or len(self.instrument_id) > 100:
            raise ValueError("invalid order instrument")
        if not isinstance(self.side, Side) or not isinstance(self.status, OrderState):
            raise ValueError("invalid typed order side or lifecycle status")
        for value in (self.original, self.filled):
            if not isinstance(value, Decimal):
                raise TypeError("order quantities must be exact Decimal")
            decimal(value)
        if self.original <= 0 or not 0 <= self.filled <= self.original:
            raise ValueError("invalid cumulative filled quantity")
        if self.status == OrderState.FILLED and self.filled != self.original:
            raise ValueError("filled order must have full cumulative quantity")
        if self.status == OrderState.WORKING and self.filled != 0:
            raise ValueError("partially filled order must be marked partial or unknown")
        if self.status == OrderState.PARTIAL and not 0 < self.filled < self.original:
            raise ValueError("partial status must retain unfilled quantity")


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    venue: str
    mode: TradingMode
    account_fingerprint: str
    observed_ns: int
    orders: tuple[RecordedOrder, ...]
    positions: tuple[tuple[str, Decimal], ...]
    collateral_balances: tuple[tuple[str, Decimal], ...]

    def __post_init__(self) -> None:
        if not re.fullmatch("[A-Z0-9]{2,16}", self.venue):
            raise ValueError("invalid venue")
        if not re.fullmatch("[a-f0-9]{64}", self.account_fingerprint):
            raise ValueError("use an opaque SHA-256 account fingerprint, not credentials")
        if type(self.observed_ns) is not int or not 0 <= self.observed_ns <= MAX_EVENT_TIMESTAMP_NS:
            raise ValueError("invalid snapshot time")
        for iid, quantity in self.positions:
            if not iid or not isinstance(quantity, Decimal):
                raise TypeError("positions require canonical instrument and exact Decimal quantity")
            decimal(quantity)
        if not self.collateral_balances:
            raise ValueError("broker reconciliation requires independent collateral evidence")
        for currency, amount in self.collateral_balances:
            if not re.fullmatch(r"[A-Z]{3,8}", currency) or not isinstance(amount, Decimal):
                raise TypeError("collateral balances require currency and exact Decimal")
            decimal(amount)


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    frozen: bool
    findings: tuple[str, ...]
    compared_orders: int
    compared_positions: int
    compared_currencies: int
    # A matching snapshot does NOT authorize execution. No live or demo
    # enablement is exposed from this pure decision oracle.
    execution_authorized: bool = False

    def require_resolved(self) -> None:
        if self.frozen:
            raise ValueError("external reconciliation unresolved: " + "; ".join(self.findings))


def _unique_orders(orders: tuple[RecordedOrder, ...], label: str) -> dict[str, RecordedOrder]:
    found = {}
    for order in orders:
        if order.client_id in found:
            raise ValueError(f"duplicate {label} client order ID")
        found[order.client_id] = order
    return found


def _unique_positions(positions: tuple[tuple[str, Decimal], ...], label: str) -> dict[str, Decimal]:
    found = {}
    for iid, quantity in positions:
        if iid in found:
            raise ValueError(f"duplicate {label} instrument position")
        found[iid] = quantity
    return found


def reconcile_demo(
    local: ReconciliationSnapshot,
    external: ReconciliationSnapshot,
    *,
    asof_ns: int,
    max_age_ns: int = 30_000_000_000,
) -> ReconciliationDecision:
    """Compare two independent views without changing either or trusting status alone.

    The *external adapter* must prove provenance, identity, region, and a
    complete position/open-order snapshot. This function only checks the
    supplied data; successful equality is NOT proof those claims are true.
    """
    if type(asof_ns) is not int or not 0 <= asof_ns <= MAX_EVENT_TIMESTAMP_NS:
        raise ValueError("invalid reconciliation clock")
    if type(max_age_ns) is not int or max_age_ns <= 0:
        raise ValueError("invalid snapshot freshness bound")
    if local.mode != TradingMode.BROKER_DEMO or external.mode != TradingMode.BROKER_DEMO:
        raise ValueError("reconciliation refuses LIVE and OFFLINE_SIM account modes")
    if (local.venue, local.account_fingerprint) != (external.venue, external.account_fingerprint):
        raise ValueError("venue/account identity mismatch: no cross-account adoption")
    if any(s.observed_ns > asof_ns or asof_ns - s.observed_ns > max_age_ns for s in (local, external)):
        raise ValueError("stale, missing or future external reconciliation evidence")
    ours = _unique_orders(local.orders, "local")
    theirs = _unique_orders(external.orders, "external")
    local_positions = _unique_positions(local.positions, "local")
    ext_positions = _unique_positions(external.positions, "external")
    local_balances = _unique_positions(local.collateral_balances, "local collateral")
    ext_balances = _unique_positions(external.collateral_balances, "external collateral")
    problems = set()
    if any(o.status == OrderState.UNKNOWN for o in (*local.orders, *external.orders)):
        problems.add("unresolved_unknown_order")
    for coid in ours.keys() | theirs.keys():
        expected = ours.get(coid)
        actual = theirs.get(coid)
        if expected is None:
            problems.add("unowned_external_order")
        elif actual is None:
            # Some venues list only open orders. Without independently
            # durable trade confirmations a missing terminal *fill* cannot
            # be proved by matching aggregate positions (offsetting fills
            # would otherwise hide a discrepancy).
            if expected.status.open_or_uncertain:
                problems.add("unobserved_open_order")
            elif expected.filled:
                problems.add("missing_terminal_fill_evidence")
        else:
            if (expected.instrument_id, expected.side, expected.original) != (
                actual.instrument_id,
                actual.side,
                actual.original,
            ):
                problems.add("order_identity_or_quantity_conflict")
            if expected.filled != actual.filled:
                problems.add("cumulative_fill_mismatch")
            if expected.status != actual.status:
                problems.add("order_lifecycle_mismatch")
    for iid in local_positions.keys() | ext_positions.keys():
        if local_positions.get(iid, Decimal("0")) != ext_positions.get(iid, Decimal("0")):
            problems.add("position_mismatch")
    # A zero collateral balance is not equivalent to a currency missing from
    # the account snapshot: no implicit FX conversion or netting is allowed.
    if local_balances != ext_balances:
        problems.add("collateral_balance_mismatch")
    return ReconciliationDecision(
        frozen=bool(problems),
        findings=tuple(sorted(problems)),
        compared_orders=len(ours.keys() | theirs.keys()),
        compared_positions=len(local_positions.keys() | ext_positions.keys()),
        compared_currencies=len(local_balances.keys() | ext_balances.keys()),
    )
