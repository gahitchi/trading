"""F1.3.c explicitly bounded USD cash-account admission policy.

The independent economic ledger also supports *research* short positions.
A CASH-account qualification must separately refuse shorting, negative
settled cash, foreign currencies and unclassified instruments. A caller's
instrument manifest is not authoritative broker instrument metadata.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from lab.economic_ledger import SYMBOL, EconomicLedgerError, _calc, exact
from lab.engine_events import (
    BrokerFillFact,
    CashFact,
    CashInLieuFact,
    EngineEconomicBook,
    EngineFactError,
    FillFact,
    SplitFact,
    _identity,
)
from lab.settlement_input import _reject_nonfinite, _unique_object

MAX_POLICY_BYTES = 64 * 1024
MAX_POLICY_INSTRUMENTS = 1024
POLICY_FIELDS = frozenset({"schema_version", "account_id", "record_id", "account_type", "currency", "instruments"})
INSTRUMENT_FIELDS = frozenset({"symbol", "asset_class", "quote_currency", "venue_class"})
ASSET_CLASSES = frozenset({"EQUITY", "ETF"})


@dataclass(frozen=True, slots=True)
class CashAccountPolicy:
    account_id: str
    source_sha256: str
    account_type: str
    currency: str
    instruments: tuple[str, ...]
    original_bytes: bytes
    locally_declared_not_broker_verified: bool = True


@dataclass(frozen=True, slots=True)
class CashAccountScopeResult:
    account_id: str
    journal_evidence_head: str
    policy_source_sha256: str
    events_checked: int
    min_settled_cash_usd: Decimal
    final_settled_cash_usd: Decimal
    supported_instruments_checked: int
    broker_account_type_authenticated: bool = False
    execution_authorized: bool = False


def parse_cash_account_policy(raw: bytes, *, expected_account_id: str) -> CashAccountPolicy:
    _identity(expected_account_id, "USD cash account scope")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_POLICY_BYTES:
        raise EngineFactError("cash-account scope requires bounded original bytes")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
    except (UnicodeError, TypeError, ValueError) as exc:
        raise EngineFactError("invalid cash-account manifest") from exc
    if type(document) is not dict or set(document) != POLICY_FIELDS:
        raise EngineFactError("cash account manifest fields incomplete or unexpected")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise EngineFactError("unsupported cash-account scope version")
    if type(document["account_id"]) is not str or document["account_id"] != expected_account_id:
        raise EngineFactError("scope account identity differs")
    if (
        type(document["account_type"]) is not str
        or document["account_type"] != "USD_UNLEVERAGED_CASH"
        or type(document["currency"]) is not str
        or document["currency"] != "USD"
    ):
        raise EngineFactError("unsupported account type, currency, leverage or margin")
    _identity(document["record_id"], "cash account scope record")
    rows = document["instruments"]
    if type(rows) is not list or len(rows) > MAX_POLICY_INSTRUMENTS:
        raise EngineFactError("unsupported cash-account instrument list")
    symbols = []
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != INSTRUMENT_FIELDS:
            raise EngineFactError("instrument metadata incomplete or unexpected")
        symbol = row["symbol"]
        if type(symbol) is not str or not SYMBOL.fullmatch(symbol) or symbol in seen:
            raise EngineFactError("untyped or duplicate cash instrument")
        if (
            type(row["asset_class"]) is not str
            or row["asset_class"] not in ASSET_CLASSES
            or type(row["quote_currency"]) is not str
            or row["quote_currency"] != "USD"
            or type(row["venue_class"]) is not str
            or row["venue_class"] != "US_LISTED"
        ):
            raise EngineFactError("unsupported instrument class, quote or venue")
        seen.add(symbol)
        symbols.append(symbol)
    return CashAccountPolicy(
        expected_account_id,
        hashlib.sha256(raw).hexdigest(),
        "USD_UNLEVERAGED_CASH",
        "USD",
        tuple(sorted(symbols)),
        raw,
    )


def validate_cash_account_scope(book: EngineEconomicBook, policy: CashAccountPolicy) -> CashAccountScopeResult:
    """Check *every* historical prefix, including now-closed short/overdraft states."""
    if type(book) is not EngineEconomicBook or type(policy) is not CashAccountPolicy:
        raise EngineFactError("cash-account check requires typed book and policy")
    if (
        type(policy.original_bytes) is not bytes
        or parse_cash_account_policy(policy.original_bytes, expected_account_id=book.account_id) != policy
    ):
        raise EngineFactError("cash-account policy was mutated after original source validation")
    if (
        book.account_id != policy.account_id
        or policy.account_type != "USD_UNLEVERAGED_CASH"
        or policy.currency != "USD"
        or type(policy.instruments) is not tuple
        or len(policy.instruments) > MAX_POLICY_INSTRUMENTS
        or len(set(policy.instruments)) != len(policy.instruments)
        or any(type(s) is not str or not SYMBOL.fullmatch(s) for s in policy.instruments)
    ):
        raise EngineFactError("foreign or invalid cash account policy")
    book.audit()
    allowed = set(policy.instruments)
    cash = Decimal(0)
    minimum_cash = Decimal(0)
    inventory: dict[str, Fraction] = {}
    seen = set()
    for fact, entry in zip(book.events, book.ledger.journal, strict=True):
        if type(fact) in (FillFact, BrokerFillFact):
            if fact.symbol not in allowed:
                raise EngineFactError("fill instrument is outside explicit cash-account scope")
            direction = Fraction(fact.quantity) if fact.side == "BUY" else -Fraction(fact.quantity)
            inventory[fact.symbol] = inventory.get(fact.symbol, Fraction(0)) + direction
            seen.add(fact.symbol)
            if inventory[fact.symbol] < 0:
                raise EngineFactError("unleveraged cash account cannot short instruments")
        elif type(fact) is SplitFact:
            if fact.symbol not in allowed:
                raise EngineFactError("corporate-action instrument is outside cash account scope")
            inventory[fact.symbol] = inventory.get(fact.symbol, Fraction(0)) * Fraction(
                fact.numerator, fact.denominator
            )
            seen.add(fact.symbol)
        elif type(fact) is CashInLieuFact:
            if fact.symbol not in allowed or fact.side != "LONG":
                raise EngineFactError("cash account refuses short or foreign cash-in-lieu")
            inventory[fact.symbol] = inventory.get(fact.symbol, Fraction(0)) - Fraction(fact.quantity)
            if inventory[fact.symbol] < 0:
                raise EngineFactError("cash-in-lieu exceeds fully paid long shares")
            seen.add(fact.symbol)
        elif type(fact) is CashFact and fact.kind in {"BORROW_FEE", "FINANCING", "SHORT_DISTRIBUTION", "BORROW_REBATE"}:
            raise EngineFactError("margin financing, short distribution and borrow events are unsupported")
        for posting in entry.postings:
            if posting.account == "cash:USD":
                try:
                    cash = exact(_calc(lambda posting=posting, cash=cash: cash + posting.amount))
                except EconomicLedgerError as exc:
                    raise EngineFactError("cash-account settled cash exceeds precision limits") from exc
                minimum_cash = min(minimum_cash, cash)
                if cash < 0:
                    raise EngineFactError("unleveraged cash account cannot draw negative settled cash")
    return CashAccountScopeResult(
        book.account_id,
        book.evidence_head,
        policy.source_sha256,
        len(book.events),
        minimum_cash,
        cash,
        len(seen),
    )
