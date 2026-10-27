"""Broker-neutral F1.2 economic event boundary for Trading Lab's own engine.

This is the canonical, offline event contract; NautilusTrader is NOT imported
or required. Every fact carries a stable, independently supplied source
reference. The module cannot authorize orders, settle a broker account,
classify unexplained balance changes, or attest upstream data authenticity.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from lab.economic_ledger import (
    IDENTIFIER,
    SYMBOL,
    EconomicLedger,
    EconomicLedgerError,
    exact,
)

CASH_KINDS = frozenset(
    {
        "CAPITAL_IN",
        "CAPITAL_OUT",
        "DIVIDEND",
        "INTEREST_CREDIT",
        "BORROW_REBATE",
        "DIVIDEND_WITHHOLDING",
        "SHORT_DISTRIBUTION",
        "FINANCING",
        "BORROW_FEE",
    }
)
CASH_SOURCE_KINDS = {
    "CAPITAL_IN": frozenset({"BANK_TRANSFER", "BROKER_CASH_ACTIVITY"}),
    "CAPITAL_OUT": frozenset({"BANK_TRANSFER", "BROKER_CASH_ACTIVITY"}),
    "DIVIDEND": frozenset({"BROKER_CASH_ACTIVITY"}),
    "INTEREST_CREDIT": frozenset({"BROKER_CASH_ACTIVITY"}),
    "BORROW_REBATE": frozenset({"BROKER_CASH_ACTIVITY"}),
    "DIVIDEND_WITHHOLDING": frozenset({"BROKER_CASH_ACTIVITY"}),
    "SHORT_DISTRIBUTION": frozenset({"BROKER_CASH_ACTIVITY"}),
    "FINANCING": frozenset({"BROKER_CASH_ACTIVITY"}),
    "BORROW_FEE": frozenset({"BROKER_CASH_ACTIVITY"}),
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class EngineFactError(ValueError):
    """Invalid, unproven, reordered or inconsistent engine economic evidence."""


@dataclass(frozen=True, slots=True)
class FillFact:
    event_id: str
    source_ref: str
    timestamp_ns: int
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal


@dataclass(frozen=True, slots=True)
class CashFact:
    event_id: str
    source_ref: str
    timestamp_ns: int
    kind: str
    amount: Decimal
    source_kind: str
    settlement_status: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class SplitFact:
    event_id: str
    source_ref: str
    timestamp_ns: int
    symbol: str
    numerator: int
    denominator: int


@dataclass(frozen=True, slots=True)
class CashInLieuFact:
    event_id: str
    source_ref: str
    timestamp_ns: int
    symbol: str
    side: str
    quantity: Decimal
    cash: Decimal
    source_kind: str
    settlement_status: str
    source_sha256: str


EconomicFact = FillFact | CashFact | SplitFact | CashInLieuFact


@dataclass(frozen=True, slots=True)
class EnginePosition:
    symbol: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class EngineSnapshot:
    """A separately obtained engine state; completeness is an explicit claim."""

    account_id: str
    as_of_ns: int
    applied_event_ids: tuple[str, ...]
    cash: Decimal
    positions: tuple[EnginePosition, ...]
    complete_instrument_scope: bool
    applied_evidence_head: str


@dataclass(frozen=True, slots=True)
class EngineParity:
    cash_delta: Decimal
    position_deltas: tuple[tuple[str, Decimal], ...]
    applied_event_ids: tuple[str, ...]
    evidence_head: str

    @property
    def matches(self) -> bool:
        return self.cash_delta == 0 and all(difference == 0 for _, difference in self.position_deltas)


def _identity(value: object, description: str) -> str:
    if type(value) is not str or not IDENTIFIER.fullmatch(value):
        raise EngineFactError(f"invalid {description}")
    return value


def _number(value: object, description: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if type(value) is not Decimal:
        raise EngineFactError(f"{description} must be exact Decimal")
    try:
        return exact(value, positive=positive, nonnegative=nonnegative)
    except EconomicLedgerError as exc:
        raise EngineFactError(f"invalid {description}") from exc


def _evidence_digest(previous: str, account_id: str, fact: EconomicFact) -> str:
    """Bind event identity, provenance, time and raw economic inputs.

    This is an unkeyed integrity checksum, not a signature or a source-proof.
    F1.4 must anchor it externally before independent custody claims.
    """
    common = {
        "account": account_id,
        "event": fact.event_id,
        "source": fact.source_ref,
        "time_ns": fact.timestamp_ns,
    }
    if type(fact) is FillFact:
        payload = {
            "kind": "FILL",
            "symbol": fact.symbol,
            "side": fact.side,
            "quantity": format(fact.quantity, "f"),
            "price": format(fact.price, "f"),
            "commission": format(fact.commission, "f"),
        }
    elif type(fact) is CashFact:
        payload = {
            "kind": fact.kind,
            "amount": format(fact.amount, "f"),
            "source_kind": fact.source_kind,
            "settlement_status": fact.settlement_status,
            "source_sha256": fact.source_sha256,
        }
    elif type(fact) is CashInLieuFact:
        payload = {
            "kind": "CASH_IN_LIEU",
            "symbol": fact.symbol,
            "side": fact.side,
            "quantity": format(fact.quantity, "f"),
            "cash": format(fact.cash, "f"),
            "source_kind": fact.source_kind,
            "settlement_status": fact.settlement_status,
            "source_sha256": fact.source_sha256,
        }
    elif type(fact) is SplitFact:
        payload = {
            "kind": "SPLIT",
            "symbol": fact.symbol,
            "numerator": fact.numerator,
            "denominator": fact.denominator,
        }
    else:
        raise EngineFactError("unsupported evidence fact")
    document = {"schema": 1, "previous": previous, **common, "business": payload}
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _event_ledger(ledger: EconomicLedger, fact: EconomicFact) -> EconomicLedger:
    try:
        if type(fact) is FillFact:
            if type(fact.symbol) is not str or not SYMBOL.fullmatch(fact.symbol):
                raise EngineFactError("invalid fill symbol")
            if fact.side not in ("BUY", "SELL"):
                raise EngineFactError("invalid fill direction")
            return ledger.post_fill(
                fact.event_id,
                fact.symbol,
                fact.side,
                _number(fact.quantity, "quantity", positive=True),
                _number(fact.price, "price", positive=True),
                commission=_number(fact.commission, "commission", nonnegative=True),
            )
        if type(fact) is CashFact:
            if fact.kind not in CASH_KINDS:
                raise EngineFactError("unsupported or unclassified cash movement")
            if type(fact.settlement_status) is not str or fact.settlement_status != "SETTLED":
                raise EngineFactError("estimated, pending or unverified cash cannot be booked as settled")
            if type(fact.source_kind) is not str or fact.source_kind not in CASH_SOURCE_KINDS[fact.kind]:
                raise EngineFactError("cash classification and source type do not agree")
            if type(fact.source_sha256) is not str or not SHA256.fullmatch(fact.source_sha256):
                raise EngineFactError("cash evidence lacks a canonical source digest")
            return ledger.post_cash(fact.event_id, fact.kind, _number(fact.amount, "cash amount", positive=True))
        if type(fact) is CashInLieuFact:
            if fact.source_kind != "BROKER_CASH_ACTIVITY" or fact.settlement_status != "SETTLED":
                raise EngineFactError("cash-in-lieu requires classified settled broker cash activity")
            if type(fact.source_sha256) is not str or not SHA256.fullmatch(fact.source_sha256):
                raise EngineFactError("cash-in-lieu source digest missing")
            return ledger.post_cash_in_lieu(
                fact.event_id,
                fact.symbol,
                fact.side,
                _number(fact.quantity, "fractional shares", positive=True),
                _number(fact.cash, "cash-in-lieu amount", positive=True),
            )
        if type(fact) is SplitFact:
            if type(fact.symbol) is not str or not SYMBOL.fullmatch(fact.symbol):
                raise EngineFactError("invalid split instrument")
            return ledger.post_split(fact.event_id, fact.symbol, fact.numerator, fact.denominator)
    except EconomicLedgerError as exc:
        raise EngineFactError("economic journal rejected source event") from exc
    raise EngineFactError("unsupported engine fact type")


@dataclass(frozen=True, slots=True)
class EngineEconomicBook:
    """Independent economic journal driven by Trading Lab's own typed facts."""

    account_id: str
    events: tuple[EconomicFact, ...] = ()
    ledger: EconomicLedger = field(default_factory=EconomicLedger)
    evidence_head: str = "0" * 64

    def append(self, fact: EconomicFact) -> EngineEconomicBook:
        _identity(self.account_id, "account identity")
        if type(fact) not in (FillFact, CashFact, SplitFact, CashInLieuFact):
            raise EngineFactError("unsupported economic event")
        _identity(fact.event_id, "event identity")
        _identity(fact.source_ref, "independent source reference")
        if type(fact.timestamp_ns) is not int or fact.timestamp_ns <= 0:
            raise EngineFactError("invalid event timestamp")
        if self.events and fact.timestamp_ns < self.events[-1].timestamp_ns:
            raise EngineFactError("out-of-order economic event")
        if any(e.event_id == fact.event_id or e.source_ref == fact.source_ref for e in self.events):
            raise EngineFactError("duplicate event or source identity")
        new_ledger = _event_ledger(self.ledger, fact)
        next_head = _evidence_digest(self.evidence_head, self.account_id, fact)
        return EngineEconomicBook(self.account_id, (*self.events, fact), new_ledger, next_head)

    def audit(self) -> None:
        """Rebuild independently from business facts, not saved ledger postings."""
        reconstructed = EngineEconomicBook(self.account_id)
        for fact in self.events:
            reconstructed = reconstructed.append(fact)
        self.ledger.audit()
        if reconstructed.ledger != self.ledger:
            raise EngineFactError("journal differs from event-source replay")
        if reconstructed.evidence_head != self.evidence_head:
            raise EngineFactError("economic source identity, time or custody chain was altered")

    def reconcile(self, snapshot: EngineSnapshot) -> EngineParity:
        """Compare a separately observed complete engine account against facts."""
        self.audit()
        if type(snapshot) is not EngineSnapshot or snapshot.account_id != self.account_id:
            raise EngineFactError("foreign or untyped engine snapshot")
        if type(snapshot.as_of_ns) is not int or snapshot.as_of_ns <= 0:
            raise EngineFactError("invalid engine observation timestamp")
        if self.events and snapshot.as_of_ns < self.events[-1].timestamp_ns:
            raise EngineFactError("stale engine observation")
        if type(snapshot.complete_instrument_scope) is not bool or not snapshot.complete_instrument_scope:
            raise EngineFactError("incomplete instrument coverage cannot establish parity")
        if type(snapshot.applied_event_ids) is not tuple or snapshot.applied_event_ids != tuple(
            fact.event_id for fact in self.events
        ):
            raise EngineFactError("engine snapshot omits, reorders or duplicates economic events")
        if snapshot.applied_evidence_head != self.evidence_head:
            raise EngineFactError("engine snapshot evidence fingerprint differs")
        if type(snapshot.positions) is not tuple or any(type(p) is not EnginePosition for p in snapshot.positions):
            raise EngineFactError("untyped engine positions")
        observed: dict[str, Decimal] = {}
        for position in snapshot.positions:
            if type(position.symbol) is not str or not SYMBOL.fullmatch(position.symbol):
                raise EngineFactError("invalid observed symbol")
            if position.symbol in observed:
                raise EngineFactError("duplicate observed instrument")
            observed[position.symbol] = _number(position.quantity, "position quantity")
        symbols = {lot.symbol for lot in self.ledger.lots}
        if symbols - observed.keys():
            raise EngineFactError("engine snapshot omitted a journal position")
        deltas = tuple((symbol, observed[symbol] - self.ledger.position(symbol)) for symbol in sorted(observed))
        return EngineParity(
            _number(snapshot.cash, "engine cash") - self.ledger.balance("cash:USD"),
            deltas,
            snapshot.applied_event_ids,
            self.evidence_head,
        )
