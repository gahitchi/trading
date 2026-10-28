"""F1.1.c: independently sourced USD reference valuation (not a cash posting).

A reference mark is untrusted evidence, not an executable market quote, a
broker valuation or proof of an available exit price. Unrealized P&L never
changes the exact double-entry cash/inventory journal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from lab.economic_ledger import SYMBOL, EconomicLedgerError, _calc, exact
from lab.engine_events import EngineEconomicBook, EngineFactError, _identity
from lab.settlement_input import _reject_nonfinite, _unique_object

MAX_MARK_BYTES = 128 * 1024
MAX_MARK_COUNT = 2_048
MARK_FIELDS = frozenset(
    {
        "schema_version",
        "account_id",
        "record_id",
        "as_of_ns",
        "source_kind",
        "quote_currency",
        "complete_instrument_scope",
        "marks",
    }
)
PRICE_FIELDS = frozenset({"symbol", "price", "observed_at_ns"})


@dataclass(frozen=True, slots=True)
class ReferenceMark:
    symbol: str
    price: Decimal
    observed_at_ns: int


@dataclass(frozen=True, slots=True)
class ReferenceMarkStatement:
    account_id: str
    source_ref: str
    source_sha256: str
    as_of_ns: int
    marks: tuple[ReferenceMark, ...]
    complete_instrument_scope: bool
    source_namespace: str
    original_bytes: bytes


@dataclass(frozen=True, slots=True)
class ValuedPosition:
    symbol: str
    quantity: Decimal
    signed_basis: Decimal
    reference_price: Decimal
    signed_market_value: Decimal
    unrealized_profit: Decimal


@dataclass(frozen=True, slots=True)
class F1ReferenceValuation:
    account_id: str
    as_of_ns: int
    statement_sha256: str
    economic_head: str
    positions: tuple[ValuedPosition, ...]
    cash_usd: Decimal
    signed_marked_position_value: Decimal
    net_marked_equity_usd: Decimal
    realized_profit_usd: Decimal
    unrealized_profit_usd: Decimal
    complete_valuations_checked: bool
    broker_source_authenticated: bool = False
    executable_market_price: bool = False
    trading_authorized: bool = False


def _price(value: object) -> Decimal:
    if type(value) is not str or not 0 < len(value) <= 80:
        raise EngineFactError("reference mark requires a bounded decimal price string")
    try:
        return exact(Decimal(value), positive=True)
    except (InvalidOperation, EconomicLedgerError) as exc:
        raise EngineFactError("invalid or rounded reference mark") from exc


def _bounded_result(value: Decimal) -> Decimal:
    try:
        return exact(value)
    except EconomicLedgerError as exc:
        raise EngineFactError("reference valuation amount exceeds exact bounded cash/position precision") from exc


def parse_reference_mark_statement(
    raw: bytes, *, source_namespace: str, expected_account_id: str
) -> ReferenceMarkStatement:
    """Parse complete, account-bound original mark bytes with no default scope."""
    _identity(source_namespace, "reference mark source")
    _identity(expected_account_id, "reference mark account")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_MARK_BYTES:
        raise EngineFactError("reference mark source must have bounded original bytes")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise EngineFactError("invalid original reference mark evidence") from exc
    if type(doc) is not dict or set(doc) != MARK_FIELDS:
        raise EngineFactError("incomplete reference mark scope or unexpected fields")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise EngineFactError("unsupported reference mark version")
    if type(doc["account_id"]) is not str or doc["account_id"] != expected_account_id:
        raise EngineFactError("reference valuation belongs to a different account")
    if type(doc["as_of_ns"]) is not int or doc["as_of_ns"] <= 0:
        raise EngineFactError("invalid reference valuation time")
    if (
        type(doc["source_kind"]) is not str
        or doc["source_kind"] != "INDEPENDENT_REFERENCE_MARKS"
        or type(doc["quote_currency"]) is not str
        or doc["quote_currency"] != "USD"
    ):
        raise EngineFactError("unsupported currency or mark source semantics")
    if type(doc["complete_instrument_scope"]) is not bool or not doc["complete_instrument_scope"]:
        raise EngineFactError("reference valuation requires an explicitly complete instrument scope")
    records = doc["marks"]
    if type(records) is not list or len(records) > MAX_MARK_COUNT:
        raise EngineFactError("reference valuation instrument count is invalid")
    marks = []
    seen = set()
    for item in records:
        if type(item) is not dict or set(item) != PRICE_FIELDS:
            raise EngineFactError("invalid reference mark fields")
        symbol = item["symbol"]
        stamp = item["observed_at_ns"]
        if type(symbol) is not str or not SYMBOL.fullmatch(symbol) or symbol in seen:
            raise EngineFactError("duplicate or invalid reference instrument")
        if type(stamp) is not int or stamp <= 0 or stamp > doc["as_of_ns"]:
            raise EngineFactError("reference mark is future-dated or untimed")
        seen.add(symbol)
        marks.append(ReferenceMark(symbol, _price(item["price"]), stamp))
    return ReferenceMarkStatement(
        account_id=expected_account_id,
        source_ref=f"{source_namespace}:{_identity(doc['record_id'], 'reference mark record')}",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        as_of_ns=doc["as_of_ns"],
        marks=tuple(marks),
        complete_instrument_scope=True,
        source_namespace=source_namespace,
        original_bytes=raw,
    )


def value_economic_book(
    book: EngineEconomicBook,
    statement: ReferenceMarkStatement,
    *,
    max_mark_age_ns: int,
) -> F1ReferenceValuation:
    """Mark FIFO lots exactly without booking profit or accepting stale prices."""
    if type(book) is not EngineEconomicBook or type(statement) is not ReferenceMarkStatement:
        raise EngineFactError("typed economic book and independent mark statement required")
    if (
        type(statement.source_namespace) is not str
        or type(statement.original_bytes) is not bytes
        or parse_reference_mark_statement(
            statement.original_bytes,
            source_namespace=statement.source_namespace,
            expected_account_id=book.account_id,
        )
        != statement
    ):
        raise EngineFactError("typed reference marks diverge from their original evidence bytes")
    if statement.account_id != book.account_id or statement.complete_instrument_scope is not True:
        raise EngineFactError("reference mark scope disagrees with journal account")
    if type(max_mark_age_ns) is not int or not 0 <= max_mark_age_ns <= 604_800_000_000_000:
        raise EngineFactError("explicit bounded maximum reference mark age required")
    if book.events and statement.as_of_ns < book.events[-1].timestamp_ns:
        raise EngineFactError("reference mark snapshot predates economic journal")
    book.audit()
    if type(statement.marks) is not tuple or len(statement.marks) > MAX_MARK_COUNT:
        raise EngineFactError("invalid typed reference mark scope")
    prices: dict[str, Decimal] = {}
    for mark in statement.marks:
        if type(mark) is not ReferenceMark or mark.symbol in prices:
            raise EngineFactError("untyped or duplicate reference valuation")
        if type(mark.observed_at_ns) is not int or not 0 < mark.observed_at_ns <= statement.as_of_ns:
            raise EngineFactError("invalid mark observation timestamp")
        if statement.as_of_ns - mark.observed_at_ns > max_mark_age_ns:
            raise EngineFactError("stale reference mark cannot establish unrealized profit")
        prices[mark.symbol] = _price(format(exact(mark.price, positive=True), "f"))
    symbols = {lot.symbol for lot in book.ledger.lots}
    if set(prices) != symbols:
        raise EngineFactError("missing or surplus price marks cannot establish complete position valuation")
    positions: list[ValuedPosition] = []
    signed_total = Decimal(0)
    unrealized_total = Decimal(0)
    for symbol in sorted(symbols):
        qty = book.ledger.position(symbol)
        basis = _bounded_result(
            _calc(
                lambda symbol=symbol: sum((lot.basis for lot in book.ledger.lots if lot.symbol == symbol), Decimal(0))
            )
        )
        marked = _bounded_result(_calc(lambda qty=qty, symbol=symbol: qty * prices[symbol]))
        unrealized = _bounded_result(_calc(lambda marked=marked, basis=basis: marked - basis))
        signed_total = _bounded_result(_calc(lambda marked=marked, signed_total=signed_total: signed_total + marked))
        unrealized_total = _bounded_result(
            _calc(lambda unrealized=unrealized, unrealized_total=unrealized_total: unrealized_total + unrealized)
        )
        positions.append(ValuedPosition(symbol, qty, basis, prices[symbol], marked, unrealized))
    cash = _bounded_result(book.ledger.balance("cash:USD"))
    realized = _bounded_result(_calc(lambda: -book.ledger.balance("income:realized")))
    net_equity = _bounded_result(_calc(lambda: cash + signed_total))
    # Independent rational check: do not reuse inventory journal arithmetic.
    rational_equity = Fraction(cash) + sum(
        (Fraction(item.quantity) * Fraction(item.reference_price) for item in positions), Fraction(0)
    )
    if Fraction(net_equity) != rational_equity:
        raise EngineFactError("reference valuation disagrees with exact rational cash/quantity calculation")
    return F1ReferenceValuation(
        book.account_id,
        statement.as_of_ns,
        statement.source_sha256,
        book.evidence_head,
        tuple(positions),
        cash,
        signed_total,
        net_equity,
        realized,
        unrealized_total,
        True,
    )
