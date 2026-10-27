"""Broker-neutral F1.2 economic event boundary for Trading Lab's own engine.

This is the canonical, offline event contract; NautilusTrader is NOT imported
or required. Every fact carries a stable, independently supplied source
reference. The module cannot authorize orders, settle a broker account,
classify unexplained balance changes, or attest upstream data authenticity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lab.economic_ledger import (
    IDENTIFIER,
    SYMBOL,
    EconomicLedger,
    EconomicLedgerError,
    exact,
)

CASH_KINDS = frozenset({"CAPITAL_IN", "CAPITAL_OUT", "DIVIDEND", "FINANCING", "BORROW_FEE"})


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


@dataclass(frozen=True, slots=True)
class SplitFact:
    event_id: str
    source_ref: str
    timestamp_ns: int
    symbol: str
    numerator: int
    denominator: int


EconomicFact = FillFact | CashFact | SplitFact


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


@dataclass(frozen=True, slots=True)
class EngineParity:
    cash_delta: Decimal
    position_deltas: tuple[tuple[str, Decimal], ...]
    applied_event_ids: tuple[str, ...]

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
            return ledger.post_cash(fact.event_id, fact.kind, _number(fact.amount, "cash amount", positive=True))
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
    ledger: EconomicLedger = EconomicLedger()

    def append(self, fact: EconomicFact) -> EngineEconomicBook:
        _identity(self.account_id, "account identity")
        if type(fact) not in (FillFact, CashFact, SplitFact):
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
        return EngineEconomicBook(self.account_id, (*self.events, fact), new_ledger)

    def audit(self) -> None:
        """Rebuild independently from business facts, not saved ledger postings."""
        reconstructed = EngineEconomicBook(self.account_id)
        for fact in self.events:
            reconstructed = reconstructed.append(fact)
        self.ledger.audit()
        if reconstructed.ledger != self.ledger:
            raise EngineFactError("journal differs from event-source replay")

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
        deltas = tuple(
            (symbol, observed[symbol] - self.ledger.position(symbol)) for symbol in sorted(observed)
        )
        return EngineParity(
            _number(snapshot.cash, "engine cash") - self.ledger.balance("cash:USD"),
            deltas,
            snapshot.applied_event_ids,
        )
