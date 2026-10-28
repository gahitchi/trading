"""F1.5 offline, independent arithmetic account projection for Trading Lab.

Uses exact rational arithmetic over the *source business facts*, rather than
re-reading journal postings or FIFO lots. Provides a second mathematical path
to detect cash/quantity conservation errors. It is not an external broker
account oracle, risk engine, or a claim of authenticated source evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from lab.economic_ledger import _calc
from lab.engine_events import (
    BrokerFillFact,
    CashFact,
    CashReversalFact,
    CashInLieuFact,
    EngineEconomicBook,
    EngineFactError,
    FillFact,
    SplitFact,
)

SOURCE_CASH_SIGN = {
    "CAPITAL_IN": 1,
    "CAPITAL_OUT": -1,
    "DIVIDEND": 1,
    "INTEREST_CREDIT": 1,
    "BORROW_REBATE": 1,
    "DIVIDEND_WITHHOLDING": -1,
    "SHORT_DISTRIBUTION": -1,
    "FINANCING": -1,
    "BORROW_FEE": -1,
}
MAX_EXACT_SCALE = 18


def _fraction_decimal(f: Fraction) -> Decimal:
    """Convert a rational to exact bounded base-10, refusing all rounding."""
    denominator = f.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    scale = max(twos, fives)
    if denominator != 1 or scale > MAX_EXACT_SCALE:
        raise EngineFactError("account projection requires explicit fractional rounding policy")
    unscaled, remainder = divmod(f.numerator * 10**scale, f.denominator)
    if remainder:
        raise EngineFactError("non-terminating fraction cannot be projected exactly")
    if len(str(abs(unscaled))) > 25:
        raise EngineFactError("account projection exceeds bounded decimal precision")
    digits = tuple(int(char) for char in str(abs(unscaled)))
    return Decimal((int(unscaled < 0), digits, -scale))


@dataclass(frozen=True, slots=True)
class ProjectedAccount:
    account_id: str
    cash: Decimal
    positions: tuple[tuple[str, Decimal], ...]
    source_event_ids: tuple[str, ...]
    evidence_head: str

    def quantity(self, symbol: str) -> Decimal:
        return dict(self.positions).get(symbol, Decimal(0))


@dataclass(frozen=True, slots=True)
class IndependentProjectionParity:
    cash_delta: Decimal
    position_deltas: tuple[tuple[str, Decimal], ...]
    events_checked: int

    @property
    def matches(self) -> bool:
        return self.cash_delta == 0 and all(delta == 0 for _, delta in self.position_deltas)


def project_account(book: EngineEconomicBook) -> ProjectedAccount:
    """Apply source facts via fractions; never use ledger positions/postings."""
    if type(book) is not EngineEconomicBook:
        raise EngineFactError("typed engine economic book required")
    book.audit()
    cash = Fraction(0)
    positions: dict[str, Fraction] = {}
    for fact in book.events:
        if type(fact) in (FillFact, BrokerFillFact):
            signed = Fraction(fact.quantity) if fact.side == "BUY" else -Fraction(fact.quantity)
            positions[fact.symbol] = positions.get(fact.symbol, Fraction(0)) + signed
            cash -= signed * Fraction(fact.price) + Fraction(fact.commission)
        elif type(fact) is CashFact:
            sign = SOURCE_CASH_SIGN.get(fact.kind)
            if sign is None:
                raise EngineFactError("projection refuses unclassified cash")
            cash += sign * Fraction(fact.amount)
        elif type(fact) is CashReversalFact:
            target = next((old for old in book.events if old.event_id == fact.target_event_id), None)
            if type(target) is not CashFact:
                raise EngineFactError("cash reversal projection lacks a prior original cash source")
            if (
                target.source_ref != fact.target_source_ref
                or target.source_sha256 != fact.target_source_sha256
                or target.kind != fact.original_kind
                or target.amount != fact.amount
            ):
                raise EngineFactError("cash reversal projection found a conflicting original fact")
            sign = SOURCE_CASH_SIGN.get(target.kind)
            if sign is None:
                raise EngineFactError("cash reversal projection refuses unclassified cash")
            cash -= sign * Fraction(target.amount)
        elif type(fact) is CashInLieuFact:
            previous = positions.get(fact.symbol, Fraction(0))
            expected_sign = 1 if fact.side == "LONG" else -1
            if previous * expected_sign < Fraction(fact.quantity):
                raise EngineFactError("cash-in-lieu exceeds long or short fractional position")
            positions[fact.symbol] = previous - expected_sign * Fraction(fact.quantity)
            cash += expected_sign * Fraction(fact.cash)
        elif type(fact) is SplitFact:
            if fact.symbol not in positions or positions[fact.symbol] == 0:
                raise EngineFactError("projection split has no owned position")
            positions[fact.symbol] *= Fraction(fact.numerator, fact.denominator)
        else:
            raise EngineFactError("projection refuses unsupported economic source")
    return ProjectedAccount(
        book.account_id,
        _fraction_decimal(cash),
        tuple((symbol, _fraction_decimal(quantity)) for symbol, quantity in sorted(positions.items()) if quantity),
        tuple(fact.event_id for fact in book.events),
        book.evidence_head,
    )


def compare_projection(book: EngineEconomicBook) -> IndependentProjectionParity:
    """Compare rational cash/quantity projection with double-entry ledger."""
    own = project_account(book)
    journal = book.ledger
    symbols = {symbol for symbol, _ in own.positions} | {lot.symbol for lot in journal.lots}
    deltas = tuple(
        (symbol, _calc(lambda symbol=symbol: own.quantity(symbol) - journal.position(symbol)))
        for symbol in sorted(symbols)
    )
    return IndependentProjectionParity(
        _calc(lambda: own.cash - journal.balance("cash:USD")),
        deltas,
        len(book.events),
    )
