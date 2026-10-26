"""Independent, exact-Decimal cash termination for delistings (F1 foundation).

This models an evidenced cash merger or final zero-recovery write-off as a
corporate action, NOT an exchange fill. A receivable/payable is distinct from
cash, and a broker-sourced receipt is required before cash moves.

IMPORTANT: this is an independent reference ledger, NOT an implementation of
NautilusTrader Portfolio/Cache/Account mutation, authenticated broker state,
or external order routing. F1 is NOT qualified until native parity is proved.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal

_ID = re.compile(r"[A-Za-z0-9_:/.-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_NS = (1 << 63) - 1
_KINDS = {"cash_merger", "bankruptcy_writeoff"}


class CorporateActionError(ValueError):
    """Corporate-action state, provenance, quantity or cashflow is unqualified."""


def _name(value: str, field: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise CorporateActionError(f"invalid {field}")
    return value


def _ns(value: int, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_NS:
        raise CorporateActionError(f"invalid {field} timestamp")
    return value


def _decimal(value: Decimal, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise CorporateActionError(f"{field} requires finite exact Decimal")
    return value


def _hash(value: str, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise CorporateActionError(f"{field} requires canonical SHA-256")
    return value


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def _currency(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Z]{3,8}", value) is None:
        raise CorporateActionError("invalid settlement currency")
    return value


@dataclass(frozen=True, slots=True)
class CashTermination:
    action_id: str
    instrument_id: str
    currency: str
    kind: str
    cash_per_unit: Decimal
    effective_ns: int
    available_ns: int
    payable_not_before_ns: int
    source_id: str
    source_sha256: str
    action_version: str

    def __post_init__(self) -> None:
        _name(self.action_id, "action ID")
        _name(self.instrument_id, "instrument ID")
        _currency(self.currency)
        _name(self.source_id, "provenance source")
        _hash(self.source_sha256, "provenance")
        _name(self.action_version, "action version")
        if self.kind not in _KINDS:
            raise CorporateActionError("unsupported economic action kind")
        amount = _decimal(self.cash_per_unit, "consideration")
        if amount < 0:
            raise CorporateActionError("negative cash consideration")
        if (self.kind == "bankruptcy_writeoff" and amount != 0) or (self.kind == "cash_merger" and amount <= 0):
            raise CorporateActionError("explicit zero writeoff and positive cash-merger must be classified separately")
        _ns(self.effective_ns, "effective")
        _ns(self.available_ns, "source availability")
        _ns(self.payable_not_before_ns, "earliest payment")
        if self.payable_not_before_ns < self.effective_ns:
            raise CorporateActionError("payment predates action")

    def record(self) -> dict:
        return {
            "action_id": self.action_id,
            "instrument_id": self.instrument_id,
            "currency": self.currency,
            "kind": self.kind,
            "cash_per_unit": str(self.cash_per_unit),
            "effective_ns": self.effective_ns,
            "available_ns": self.available_ns,
            "payable_not_before_ns": self.payable_not_before_ns,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "action_version": self.action_version,
        }

    @classmethod
    def from_record(cls, d: dict) -> CashTermination:
        if type(d) is not dict or set(d) != set(cls.__dataclass_fields__):
            raise CorporateActionError("noncanonical action record")
        if type(d["cash_per_unit"]) is not str:
            raise CorporateActionError("cash consideration must be an exact decimal string")
        try:
            return cls(**{**d, "cash_per_unit": Decimal(d["cash_per_unit"])})
        except (ValueError, ArithmeticError) as exc:
            raise CorporateActionError("unparseable cash consideration") from exc


@dataclass(frozen=True, slots=True)
class ReconciledBook:
    """Explicit snapshot assertions. Broker authentication occurs elsewhere."""

    position: Decimal
    positions_verified: bool
    orders_verified: bool
    open_order_ids: tuple[str, ...]
    action_version: str

    def __post_init__(self) -> None:
        _decimal(self.position, "reconciled position")
        if type(self.positions_verified) is not bool or type(self.orders_verified) is not bool:
            raise CorporateActionError("reconciliation evidence flags must be booleans")
        if type(self.open_order_ids) is not tuple:
            raise CorporateActionError("open order IDs must be a tuple")
        for oid in self.open_order_ids:
            _name(oid, "open order ID")
        if len(set(self.open_order_ids)) != len(self.open_order_ids):
            raise CorporateActionError("duplicate broker order ID")
        _name(self.action_version, "reconciled action version")

    def record(self) -> dict:
        return {
            "position": str(self.position),
            "positions_verified": self.positions_verified,
            "orders_verified": self.orders_verified,
            "open_order_ids": list(self.open_order_ids),
            "action_version": self.action_version,
        }

    @classmethod
    def from_record(cls, d: dict) -> ReconciledBook:
        if type(d) is not dict or set(d) != set(cls.__dataclass_fields__):
            raise CorporateActionError("noncanonical reconciliation record")
        if type(d["position"]) is not str or type(d["open_order_ids"]) is not list:
            raise CorporateActionError("reconciled position must be a decimal string, orders a list")
        try:
            position = Decimal(d["position"])
        except (ValueError, ArithmeticError) as exc:
            raise CorporateActionError("unparseable reconciled position") from exc
        return cls(
            position=position,
            positions_verified=d["positions_verified"],
            orders_verified=d["orders_verified"],
            open_order_ids=tuple(d["open_order_ids"]),
            action_version=d["action_version"],
        )


@dataclass(frozen=True, slots=True)
class CashReceipt:
    receipt_id: str
    action_id: str
    signed_amount: Decimal
    available_ns: int
    source_id: str
    source_sha256: str

    def __post_init__(self) -> None:
        _name(self.receipt_id, "receipt ID")
        _name(self.action_id, "receipt action ID")
        if not _decimal(self.signed_amount, "receipt amount"):
            raise CorporateActionError("zero-value transfer is not an executed cash receipt")
        _ns(self.available_ns, "receipt availability")
        _name(self.source_id, "cash receipt source")
        _hash(self.source_sha256, "cash receipt evidence")

    def record(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "signed_amount": str(self.signed_amount),
            "available_ns": self.available_ns,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_record(cls, d: dict) -> CashReceipt:
        if type(d) is not dict or set(d) != set(cls.__dataclass_fields__):
            raise CorporateActionError("noncanonical cash receipt record")
        if type(d["signed_amount"]) is not str:
            raise CorporateActionError("cash receipt amount must be an exact decimal string")
        try:
            return cls(**{**d, "signed_amount": Decimal(d["signed_amount"])})
        except (ValueError, ArithmeticError) as exc:
            raise CorporateActionError("unparseable receipt amount") from exc


@dataclass(frozen=True, slots=True)
class CorporateBook:
    """Independent equity-style book; cash is actual, pending is NOT cash.

    signed_basis is the historic signed share cost in the settlement currency:
    positive for held longs and negative for held shorts. A corporate action
    realizes consideration minus signed_basis, but moves no cash until proven
    receipt. The model intentionally excludes dividends, borrow and broker
    fees (which must be accounted for independently in F1).
    """

    instrument_id: str
    currency: str
    quantum: Decimal
    position: Decimal
    signed_basis: Decimal
    cash: Decimal
    realized_pnl: Decimal = Decimal("0")
    pending_cash: Decimal = Decimal("0")
    action_id: str | None = None
    action_digest: str | None = None
    payable_ns: int | None = None
    action_version: str | None = None
    last_observed_ns: int = 0
    receipt_digests: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _name(self.instrument_id, "book instrument")
        _currency(self.currency)
        quantum = _decimal(self.quantum, "currency quantum")
        if quantum <= 0 or quantum != Decimal(1).scaleb(quantum.as_tuple().exponent):
            raise CorporateActionError("currency quantum must be a positive decimal power of ten")
        for field in ("position", "signed_basis", "cash", "realized_pnl", "pending_cash"):
            _decimal(getattr(self, field), field)
        if self.position == 0 and self.signed_basis != 0:
            raise CorporateActionError("flat book cannot retain position cost basis")
        if self.position and self.position * self.signed_basis <= 0:
            raise CorporateActionError("signed cost basis must match nonzero position sign")
        for field in ("cash", "realized_pnl", "pending_cash", "signed_basis"):
            if getattr(self, field) % self.quantum:
                raise CorporateActionError(f"{field} is not representable at verified currency precision")
        _ns(self.last_observed_ns, "last observation")
        if self.action_id is None:
            if self.action_digest is not None or self.payable_ns is not None or self.action_version is not None:
                raise CorporateActionError("unrecognized action has settlement metadata")
            if self.pending_cash or self.receipt_digests:
                raise CorporateActionError("unrecognized action has receipt or payable")
        else:
            _name(self.action_id, "book action ID")
            _hash(self.action_digest, "book action digest")
            _ns(self.payable_ns, "book payable")
            _name(self.action_version, "book action version")
            if self.position or self.signed_basis:
                raise CorporateActionError("recognized corporate action must retire equity units")
        ids = []
        for entry in self.receipt_digests:
            if type(entry) is not tuple or len(entry) != 2:
                raise CorporateActionError("invalid receipt history")
            rid, digest = entry
            ids.append(_name(rid, "prior receipt ID"))
            _hash(digest, "receipt digest")
        if len(ids) != len(set(ids)):
            raise CorporateActionError("duplicate receipt history")

    def record(self) -> dict:
        return {
            "instrument_id": self.instrument_id,
            "currency": self.currency,
            "quantum": str(self.quantum),
            "position": str(self.position),
            "signed_basis": str(self.signed_basis),
            "cash": str(self.cash),
            "realized_pnl": str(self.realized_pnl),
            "pending_cash": str(self.pending_cash),
            "action_id": self.action_id,
            "action_digest": self.action_digest,
            "payable_ns": self.payable_ns,
            "action_version": self.action_version,
            "last_observed_ns": self.last_observed_ns,
            "receipt_digests": [list(x) for x in self.receipt_digests],
        }


def recognize(
    book: CorporateBook,
    action: CashTermination,
    reconciliation: ReconciledBook,
    *,
    observed_ns: int,
) -> CorporateBook:
    """Remove an independently verified holding and recognize cash due only.

    This is not safe to connect to a broker until its authenticated native
    position/cache/account adjustment is implemented and verified in F1.
    """
    _ns(observed_ns, "processing observation")
    if book.action_id is not None:
        raise CorporateActionError("action already recognized; cannot retire twice")
    if book.instrument_id != action.instrument_id or book.currency != action.currency:
        raise CorporateActionError("instrument or settlement currency differs")
    if not book.position:
        raise CorporateActionError("no holding to settle")
    if observed_ns < max(book.last_observed_ns, action.effective_ns, action.available_ns):
        raise CorporateActionError("corporate-action source is future or event observation regressed")
    if (
        not reconciliation.positions_verified
        or not reconciliation.orders_verified
        or reconciliation.open_order_ids
        or reconciliation.position != book.position
        or reconciliation.action_version != action.action_version
    ):
        raise CorporateActionError("unreconciled positions, GTC orders or action version; freeze automated action")
    due = book.position * action.cash_per_unit
    if due % book.quantum:
        raise CorporateActionError("fractional cash entitlement needs authoritative cash-in-lieu and rounding evidence")
    return replace(
        book,
        position=Decimal("0"),
        signed_basis=Decimal("0"),
        realized_pnl=book.realized_pnl + due - book.signed_basis,
        pending_cash=due,
        action_id=action.action_id,
        action_digest=_digest(action.record()),
        payable_ns=action.payable_not_before_ns,
        action_version=action.action_version,
        last_observed_ns=observed_ns,
    )


def receive(book: CorporateBook, receipt: CashReceipt, *, observed_ns: int) -> CorporateBook:
    """Apply an independently evidenced cash receipt/debit, never a market fill."""
    _ns(observed_ns, "cash observation")
    if book.action_id is None or receipt.action_id != book.action_id:
        raise CorporateActionError("cash receipt is not bound to a recognized corporate action")
    digest = _digest(receipt.record())
    for rid, prior_digest in book.receipt_digests:
        if rid == receipt.receipt_id:
            if prior_digest != digest:
                raise CorporateActionError("receipt identity replayed with conflicting monetary evidence")
            return book  # exactly-once effect even if upstream delivery is at-least-once
    if observed_ns < max(book.last_observed_ns, receipt.available_ns):
        raise CorporateActionError("cash receipt not available yet or observation regressed")
    if receipt.available_ns < book.payable_ns:
        raise CorporateActionError("cash transfer predates earliest documented settlement")
    if book.pending_cash == 0 or receipt.signed_amount * book.pending_cash <= 0:
        raise CorporateActionError("receipt/debit does not match outstanding payable direction")
    if abs(receipt.signed_amount) > abs(book.pending_cash):
        raise CorporateActionError("cash receipt exceeds unsettled obligation")
    if receipt.signed_amount % book.quantum:
        raise CorporateActionError("receipt exceeds currency precision")
    return replace(
        book,
        cash=book.cash + receipt.signed_amount,
        pending_cash=book.pending_cash - receipt.signed_amount,
        last_observed_ns=observed_ns,
        receipt_digests=(*book.receipt_digests, (receipt.receipt_id, digest)),
    )
