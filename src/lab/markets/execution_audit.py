"""Independent, exact-Decimal audit of *observed* linear contract cashflows.

Consumes order plans and previously observed fills / offline funding evidence.
This module has no matching engine, allocator, venue client or authority to
submit orders. Native Nautilus fill callbacks can be mapped into RecordedFill,
but simulated funding fixtures never authenticate real exchange settlement.

Partial fills are individually fee-rounded in the quote currency; cumulative
quantity is checked against the immutable intended order. Funding settles
against the position existing at that event, not the final net position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from lab.markets.accounting import linear_transition, signed_funding_payment
from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, Instrument, Side, decimal, positive

# A native Nautilus ClientOrderId may contain separators and exceed the
# stricter local OrderIntent alphanumeric limit. This auditor consumes an
# *observed opaque identifier*; it must not weaken local order creation.
_ORDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_MAX_EVENTS = 10_000
_DEFAULT_MARK_AGE_NS = 120_000_000_000


class AuditMismatch(ValueError):
    """Independent cash, quantity or lifecycle evidence disagrees."""


@dataclass(frozen=True, slots=True)
class AuditOrder:
    client_id: str
    side: Side
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class RecordedFill:
    client_id: str
    trade_id: str
    side: Side
    quantity: Decimal
    price: Decimal
    charged_fee: Decimal
    observed_ns: int
    liquidity: str = "taker"


@dataclass(frozen=True, slots=True)
class RecordedFunding:
    event_id: str
    event_ns: int
    observed_ns: int
    mark_ns: int
    mark_price: Decimal
    rate: Decimal
    reported_debit: Decimal
    source: str = "offline_funding_fixture"


@dataclass(frozen=True, slots=True)
class ExecutionAudit:
    final_position: Decimal
    final_entry_price: Decimal
    realized_pnl: Decimal
    charged_fees: Decimal
    funding_debits: Decimal
    ending_balance: Decimal
    remaining_orders: tuple[tuple[str, Decimal], ...]
    fill_count: int
    funding_count: int
    currency_precision: int
    source: str = "offline_independent_cashflow_oracle"
    broker_account_reconciled: bool = False
    execution_authorized: bool = False


def _money(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def audit_observed_cashflows(
    instrument: Instrument,
    orders: tuple[AuditOrder, ...],
    events: tuple[RecordedFill | RecordedFunding, ...],
    *,
    collateral: Decimal,
    taker_fee_bps: Decimal,
    maker_fee_bps: Decimal | None = None,
    currency_precision: int = 2,
    max_mark_age_ns: int = _DEFAULT_MARK_AGE_NS,
) -> ExecutionAudit:
    """Require exact order attribution, fee charges and signed funding parity.

    This intentionally does not infer unseen fills, authenticate funding
    receipts, approximate execution queue position, or authorize new orders.
    """
    if type(currency_precision) is not int or not 0 <= currency_precision <= 9:
        raise ValueError("unsupported collateral currency precision")
    if type(max_mark_age_ns) is not int or max_mark_age_ns <= 0:
        raise ValueError("invalid mark freshness bound")
    if type(collateral) is not Decimal or type(taker_fee_bps) is not Decimal:
        raise TypeError("collateral and fee rate must be exact Decimal")
    positive(decimal(collateral), "starting collateral")
    decimal(taker_fee_bps)
    if not Decimal("0") <= taker_fee_bps <= Decimal("1000"):
        raise ValueError("unsupported taker fee assumption")
    if maker_fee_bps is not None:
        if type(maker_fee_bps) is not Decimal:
            raise TypeError("maker fee rate must be exact Decimal")
        decimal(maker_fee_bps)
        if not Decimal("-100") <= maker_fee_bps <= Decimal("1000"):
            raise ValueError("unsupported maker fee or rebate assumption")
    quantum = Decimal("1").scaleb(-currency_precision)
    if _money(collateral, quantum) != collateral:
        raise AuditMismatch("starting collateral is not representable in quote currency")
    if len(events) > _MAX_EVENTS:
        raise ValueError("too many observed cashflow events")

    remaining: dict[str, Decimal] = {}
    plans: dict[str, AuditOrder] = {}
    for plan in orders:
        if type(plan) is not AuditOrder or not _ORDER_ID.fullmatch(plan.client_id):
            raise AuditMismatch("invalid durable order identity")
        if type(plan.side) is not Side or type(plan.quantity) is not Decimal:
            raise AuditMismatch("invalid durable order economics")
        instrument.check_quantity(plan.quantity)
        if plan.client_id in plans:
            raise AuditMismatch("duplicate durable order identity")
        plans[plan.client_id] = plan
        remaining[plan.client_id] = plan.quantity

    position = Decimal("0")
    entry = Decimal("0")
    realized = Decimal("0")
    fees = Decimal("0")
    funding = Decimal("0")
    last_received = -1
    last_effective = -1
    seen_fills: set[str] = set()
    seen_funding: set[str] = set()
    fill_count = 0
    funding_count = 0
    for event in events:
        if type(event) not in (RecordedFill, RecordedFunding):
            raise TypeError("only explicit fill and settled offline funding evidence is supported")
        if type(event.observed_ns) is not int or not 0 <= event.observed_ns <= MAX_EVENT_TIMESTAMP_NS:
            raise AuditMismatch("invalid observation timestamp")
        if event.observed_ns < last_received:
            raise AuditMismatch("cashflow receipt order regressed")
        last_received = event.observed_ns
        effective_ns = event.observed_ns if type(event) is RecordedFill else event.event_ns
        if type(effective_ns) is not int or effective_ns < last_effective:
            raise AuditMismatch("funding or fill event time regressed")
        last_effective = effective_ns
        if type(event) is RecordedFill:
            if (
                type(event.client_id) is not str
                or type(event.trade_id) is not str
                or not event.trade_id
                or event.trade_id in seen_fills
            ):
                raise AuditMismatch("missing or duplicated trade identity")
            seen_fills.add(event.trade_id)
            plan = plans.get(event.client_id)
            if plan is None or event.side is not plan.side:
                raise AuditMismatch("unowned fill or wrong order side")
            if type(event.quantity) is not Decimal or type(event.price) is not Decimal:
                raise AuditMismatch("fill price and size must be exact Decimal")
            if type(event.charged_fee) is not Decimal:
                raise AuditMismatch("fill fee must be exact Decimal")
            positive(decimal(event.quantity), "filled quantity")
            instrument.check_price(event.price)
            if event.quantity % instrument.quantity_step or event.quantity > remaining[event.client_id]:
                raise AuditMismatch("partial fill exceeds order remaining or quantity step")
            decimal(event.charged_fee)
            if type(event.liquidity) is not str or event.liquidity not in ("maker", "taker"):
                raise AuditMismatch("unqualified maker/taker liquidity attribution")
            if event.liquidity == "maker" and maker_fee_bps is None:
                raise AuditMismatch("missing explicit maker fee schedule for observed maker fill")
            rate = maker_fee_bps if event.liquidity == "maker" else taker_fee_bps
            expected_fee = _money(
                event.quantity * event.price * instrument.multiplier * rate / Decimal("10000"),
                quantum,
            )
            if event.charged_fee != expected_fee:
                raise AuditMismatch(
                    f"observed {event.liquidity} fee differs from independent currency-rounded schedule"
                )
            signed_qty = event.quantity * event.side.sign
            transition = linear_transition(position, entry, signed_qty, event.price, instrument.multiplier)
            position, entry = transition.position, transition.entry_price
            realized += transition.realized_pnl
            fees += event.charged_fee
            remaining[event.client_id] -= event.quantity
            fill_count += 1
        else:
            if event.source != "offline_funding_fixture":
                raise AuditMismatch("unqualified funding source: no authenticated broker evidence")
            if type(event.event_id) is not str or not event.event_id or event.event_id in seen_funding:
                raise AuditMismatch("missing or duplicated funding event identity")
            seen_funding.add(event.event_id)
            if (
                type(event.event_ns) is not int
                or type(event.mark_ns) is not int
                or not 0 <= event.mark_ns <= event.event_ns <= event.observed_ns
                or event.event_ns - event.mark_ns > max_mark_age_ns
            ):
                raise AuditMismatch("funding mark stale, future or invalid settlement time")
            if any(type(v) is not Decimal for v in (event.mark_price, event.rate, event.reported_debit)):
                raise AuditMismatch("funding economics must be exact Decimal")
            positive(decimal(event.mark_price), "funding mark")
            decimal(event.rate)
            decimal(event.reported_debit)
            if abs(event.rate) > Decimal("1"):
                raise AuditMismatch("implausible funding fraction")
            expected = _money(
                signed_funding_payment(position, event.mark_price, instrument.multiplier, event.rate),
                quantum,
            )
            if event.reported_debit != expected:
                raise AuditMismatch("settled funding cashflow differs from signed-position oracle")
            funding += event.reported_debit
            funding_count += 1
    return ExecutionAudit(
        final_position=position,
        final_entry_price=entry,
        realized_pnl=realized,
        charged_fees=fees,
        funding_debits=funding,
        ending_balance=_money(collateral + realized - fees - funding, quantum),
        remaining_orders=tuple(sorted(remaining.items())),
        fill_count=fill_count,
        funding_count=funding_count,
        currency_precision=currency_precision,
    )
