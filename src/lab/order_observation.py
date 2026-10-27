"""F1.5 independent broker-order observation against original execution facts.

A read-only quantitative completeness cross-check: it cannot prove broker
origin, order acceptance, finality or account authorization. A complete order
scope is an assertion in external data, not a verified custody property.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from lab.economic_ledger import SYMBOL, EconomicLedgerError, _calc, exact
from lab.engine_events import (
    SHA256,
    BrokerFillFact,
    EngineEconomicBook,
    EngineFactError,
    FillFact,
    _identity,
)
from lab.settlement_input import _reject_nonfinite, _unique_object

MAX_ORDER_REPORT_BYTES = 128 * 1024
MAX_ORDER_COUNT = 2048
FIELDS = frozenset(
    {
        "schema_version",
        "account_id",
        "record_id",
        "as_of_ns",
        "source_kind",
        "complete_order_scope",
        "orders",
    }
)
ORDER_FIELDS = frozenset(
    {
        "broker_order_id",
        "symbol",
        "side",
        "status",
        "requested_quantity",
        "executed_quantity",
        "remaining_quantity",
    }
)
STATES = frozenset({"ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED"})
ORDER_FIELDS_V2 = ORDER_FIELDS | {"execution_receipts"}
RECEIPT_FIELDS = frozenset({"record_id", "quantity", "source_sha256"})
MAX_ORDER_RECEIPTS = 8192


@dataclass(frozen=True, slots=True)
class ReportedExecution:
    """An externally enumerated execution report, not a broker authentication."""

    source_ref: str
    quantity: Decimal
    source_sha256: str


@dataclass(frozen=True, slots=True)
class ReportedOrder:
    broker_order_id: str
    symbol: str
    side: str
    status: str
    requested_quantity: Decimal
    executed_quantity: Decimal
    remaining_quantity: Decimal
    execution_receipts: tuple[ReportedExecution, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalOrderStatement:
    account_id: str
    source_ref: str
    as_of_ns: int
    orders: tuple[ReportedOrder, ...]
    complete_order_scope: bool
    source_sha256: str
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class OrderQuantityDelta:
    broker_order_id: str
    report_quantity: Decimal
    custody_quantity: Decimal
    delta: Decimal


@dataclass(frozen=True, slots=True)
class ExternalOrderParity:
    account_id: str
    statement_source_ref: str
    statement_sha256: str
    journal_evidence_head: str
    order_deltas: tuple[OrderQuantityDelta, ...]
    broker_fills_checked: int
    receipt_scope_checked: bool = False
    receipt_identities_checked: int = 0

    @property
    def matches(self) -> bool:
        """Reported quantities agree with stored receipt facts, not broker finality."""
        return all(item.delta == 0 for item in self.order_deltas)


def _quantity(value: object, *, positive: bool = False) -> Decimal:
    if type(value) is not str or len(value) > 80:
        raise EngineFactError("order report quantities must use bounded decimal strings")
    try:
        return exact(Decimal(value), positive=positive, nonnegative=not positive)
    except (InvalidOperation, EconomicLedgerError) as exc:
        raise EngineFactError("invalid order quantity precision or range") from exc


def parse_external_order_statement(
    raw: bytes, *, source_namespace: str, expected_account_id: str
) -> ExternalOrderStatement:
    """Parse independently supplied order status summary, rejecting ambiguity."""
    _identity(source_namespace, "order source namespace")
    _identity(expected_account_id, "expected order account")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ORDER_REPORT_BYTES:
        raise EngineFactError("order observation must contain bounded original bytes")
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EngineFactError("invalid order observation JSON") from exc
    if type(data) is not dict or set(data) != FIELDS:
        raise EngineFactError("incomplete or unexpected order observation fields")
    if type(data["schema_version"]) is not int or data["schema_version"] not in (1, 2):
        raise EngineFactError("unsupported order observation schema")
    if type(data["account_id"]) is not str or data["account_id"] != expected_account_id:
        raise EngineFactError("foreign order statement account")
    if type(data["source_kind"]) is not str or data["source_kind"] != "BROKER_ORDER_STATEMENT":
        raise EngineFactError("unsupported order report origin declaration")
    if data["complete_order_scope"] is not True:
        raise EngineFactError("incomplete order scope cannot establish parity")
    if type(data["as_of_ns"]) is not int or data["as_of_ns"] <= 0:
        raise EngineFactError("invalid order observation timestamp")
    if type(data["orders"]) is not list or len(data["orders"]) > MAX_ORDER_COUNT:
        raise EngineFactError("order observation list is unbounded or invalid")
    ids: set[str] = set()
    receipt_ids: set[str] = set()
    receipt_count = 0
    orders: list[ReportedOrder] = []
    expected_fields = ORDER_FIELDS_V2 if data["schema_version"] == 2 else ORDER_FIELDS
    for item in data["orders"]:
        if type(item) is not dict or set(item) != expected_fields:
            raise EngineFactError("incomplete or unexpected order fields")
        order_id = _identity(item["broker_order_id"], "observed order identity")
        symbol = item["symbol"]
        if order_id in ids or type(symbol) is not str or not SYMBOL.fullmatch(symbol):
            raise EngineFactError("duplicate order identity or invalid instrument")
        if type(item["side"]) is not str or item["side"] not in ("BUY", "SELL"):
            raise EngineFactError("invalid observed order side")
        if type(item["status"]) is not str or item["status"] not in STATES:
            raise EngineFactError("ambiguous or unsupported observed order state")
        requested = _quantity(item["requested_quantity"], positive=True)
        executed = _quantity(item["executed_quantity"])
        remaining = _quantity(item["remaining_quantity"])
        if executed > requested or remaining > requested:
            raise EngineFactError("order report contains an overfill or invalid remainder")
        if item["status"] == "FILLED":
            consistent = executed == requested and remaining == 0
        elif item["status"] == "PARTIALLY_FILLED":
            consistent = (
                0 < executed < requested
                and _calc(lambda requested=requested, executed=executed: requested - executed) == remaining
            )
        elif item["status"] == "ACCEPTED":
            consistent = executed == 0 and remaining == requested
        elif item["status"] == "REJECTED":
            consistent = executed == 0 and remaining == 0
        else:
            # Canceled with some executed quantity has zero *open* remainder.
            consistent = executed < requested and remaining == 0
        if not consistent:
            raise EngineFactError("reported order quantities contradict its state")
        receipts: list[ReportedExecution] = []
        if data["schema_version"] == 2:
            supplied = item["execution_receipts"]
            if type(supplied) is not list or len(supplied) > MAX_ORDER_RECEIPTS - receipt_count:
                raise EngineFactError("execution receipt scope is invalid or exceeds the bounded limit")
            for receipt in supplied:
                if type(receipt) is not dict or set(receipt) != RECEIPT_FIELDS:
                    raise EngineFactError("execution receipt has missing or unexpected fields")
                receipt_id = _identity(receipt["record_id"], "execution receipt identity")
                source_ref = f"{source_namespace}:{receipt_id}"
                if source_ref in receipt_ids:
                    raise EngineFactError("duplicate execution receipt identity in external order scope")
                digest = receipt["source_sha256"]
                if type(digest) is not str or not SHA256.fullmatch(digest):
                    raise EngineFactError("invalid execution receipt digest")
                receipts.append(
                    ReportedExecution(source_ref, _quantity(receipt["quantity"], positive=True), digest)
                )
                receipt_ids.add(source_ref)
                receipt_count += 1
            receipt_total = _calc(lambda receipts=tuple(receipts): sum((receipt.quantity for receipt in receipts), Decimal(0)))
            if receipt_total != executed:
                raise EngineFactError("execution receipt quantities do not conserve reported executed quantity")
        ids.add(order_id)
        orders.append(
            ReportedOrder(
                order_id, symbol, item["side"], item["status"], requested, executed, remaining, tuple(receipts)
            )
        )
    return ExternalOrderStatement(
        account_id=expected_account_id,
        source_ref=f"{source_namespace}:{_identity(data['record_id'], 'order observation identity')}",
        as_of_ns=data["as_of_ns"],
        orders=tuple(sorted(orders, key=lambda order: order.broker_order_id)),
        complete_order_scope=True,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        schema_version=data["schema_version"],
    )


def reconcile_external_orders(book: EngineEconomicBook, report: ExternalOrderStatement) -> ExternalOrderParity:
    """Compare original broker-fill facts with independently supplied order scope.

    A reported execution omitted from our journal becomes a nonzero delta;
    an unreported order referenced by our fills is a hard incompleteness
    error. Simulator fills cannot establish real broker-order parity.
    """
    if type(book) is not EngineEconomicBook or type(report) is not ExternalOrderStatement:
        raise EngineFactError("typed own-engine book and external order scope required")
    book.audit()
    if book.account_id != report.account_id or report.complete_order_scope is not True:
        raise EngineFactError("foreign or incomplete order observation")
    if type(report.as_of_ns) is not int or report.as_of_ns <= 0:
        raise EngineFactError("invalid order observation time")
    if book.events and report.as_of_ns < book.events[-1].timestamp_ns:
        raise EngineFactError("stale order observation")
    if type(report.schema_version) is not int or report.schema_version not in (1, 2):
        raise EngineFactError("unsupported order report version")
    if type(report.orders) is not tuple or any(type(order) is not ReportedOrder for order in report.orders):
        raise EngineFactError("untyped reported order items")
    known: dict[str, ReportedOrder] = {}
    for order in report.orders:
        if order.broker_order_id in known:
            raise EngineFactError("repeated order identity in supplied summary")
        if type(order.status) is not str or order.status not in STATES:
            raise EngineFactError("unsupported order state in supplied summary")
        known[order.broker_order_id] = order

    claimed_receipts: dict[str, tuple[str, ReportedExecution]] = {}
    if report.schema_version == 2:
        for order in report.orders:
            if type(order.execution_receipts) is not tuple:
                raise EngineFactError("untyped complete execution receipt scope")
            for receipt in order.execution_receipts:
                if type(receipt) is not ReportedExecution:
                    raise EngineFactError("untyped execution receipt")
                _identity(receipt.source_ref, "execution receipt reference")
                if type(receipt.source_sha256) is not str or not SHA256.fullmatch(receipt.source_sha256):
                    raise EngineFactError("invalid execution receipt digest")
                if receipt.source_ref in claimed_receipts:
                    raise EngineFactError("duplicate execution receipt in supplied scope")
                claimed_receipts[receipt.source_ref] = (order.broker_order_id, receipt)

    custody: dict[str, Decimal] = {}
    broker_fills_checked = 0
    for fact in book.events:
        if type(fact) is FillFact:
            raise EngineFactError("simulator executions cannot prove broker order custody")
        if type(fact) is BrokerFillFact:
            order = known.get(fact.broker_order_id)
            if order is None:
                raise EngineFactError("broker execution absent from claimed complete order scope")
            if order.symbol != fact.symbol or order.side != fact.side:
                raise EngineFactError("broker execution instrument or side disagrees with external order")
            if fact.timestamp_ns > report.as_of_ns:
                raise EngineFactError("future execution cannot match older account statement")
            if report.schema_version == 2:
                matching = claimed_receipts.pop(fact.source_ref, None)
                if matching is None:
                    raise EngineFactError("journal execution is missing from complete receipt identity scope")
                reported_order_id, receipt = matching
                if reported_order_id != fact.broker_order_id:
                    raise EngineFactError("execution receipt belongs to the wrong broker order")
                if receipt.quantity != fact.quantity or receipt.source_sha256 != fact.source_sha256:
                    raise EngineFactError("execution receipt quantity or source digest disagrees with journal")
            broker_fills_checked += 1
            custody[fact.broker_order_id] = _calc(
                lambda fact=fact: custody.get(fact.broker_order_id, Decimal(0)) + fact.quantity
            )
    if report.schema_version == 2 and claimed_receipts:
        raise EngineFactError("external receipt inventory includes executions missing from the journal")
    deltas = tuple(
        OrderQuantityDelta(
            order.broker_order_id,
            order.executed_quantity,
            custody.get(order.broker_order_id, Decimal(0)),
            _calc(lambda order=order: order.executed_quantity - custody.get(order.broker_order_id, Decimal(0))),
        )
        for order in report.orders
    )
    return ExternalOrderParity(
        account_id=book.account_id,
        statement_source_ref=report.source_ref,
        statement_sha256=report.source_sha256,
        journal_evidence_head=book.evidence_head,
        order_deltas=deltas,
        broker_fills_checked=broker_fills_checked,
        receipt_scope_checked=report.schema_version == 2,
        receipt_identities_checked=broker_fills_checked if report.schema_version == 2 else 0,
    )
