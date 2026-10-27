"""F1.2 external account arithmetic observation, independent of NautilusTrader.

The input is an *externally supplied* account statement, not a snapshot
generated from our journal. It is intentionally read-only, USD cash only.
An exact numerical match is not proof that a broker authored the statement,
that every broker event has arrived, or that the reported state is final.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from lab.account_projection import compare_projection, project_account
from lab.economic_ledger import SYMBOL, EconomicLedgerError, _calc, exact
from lab.engine_events import EngineEconomicBook, EngineFactError, _identity
from lab.settlement_input import _reject_nonfinite, _unique_object

MAX_STATEMENT_BYTES = 128 * 1024
REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "account_id",
        "record_id",
        "as_of_ns",
        "source_kind",
        "currency",
        "cash",
        "positions",
        "complete_instrument_scope",
    }
)
POSITION_FIELDS = frozenset({"symbol", "quantity"})


@dataclass(frozen=True, slots=True)
class ExternalAccountStatement:
    account_id: str
    source_ref: str
    as_of_ns: int
    cash: Decimal
    positions: tuple[tuple[str, Decimal], ...]
    complete_instrument_scope: bool
    source_sha256: str


@dataclass(frozen=True, slots=True)
class ExternalAccountParity:
    account_id: str
    statement_source_ref: str
    statement_sha256: str
    journal_evidence_head: str
    cash_delta: Decimal
    position_deltas: tuple[tuple[str, Decimal], ...]
    internal_projection_matches: bool

    @property
    def matches(self) -> bool:
        """Numerical consistency only; NOT authenticated broker settlement."""
        return (
            self.internal_projection_matches
            and self.cash_delta == 0
            and all(delta == 0 for _, delta in self.position_deltas)
        )


def _decimal_string(value: object, label: str) -> Decimal:
    if type(value) is not str or len(value) > 80:
        raise EngineFactError(f"{label} must be a bounded exact decimal string")
    try:
        return exact(Decimal(value))
    except (InvalidOperation, EconomicLedgerError) as exc:
        raise EngineFactError(f"invalid {label} precision or range") from exc


def parse_external_account_statement(
    raw: bytes, *, source_namespace: str, expected_account_id: str
) -> ExternalAccountStatement:
    """Parse a bounded, strictly scoped external statement as untrusted data."""
    _identity(source_namespace, "external statement source")
    _identity(expected_account_id, "expected account")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_STATEMENT_BYTES:
        raise EngineFactError("external account statement bytes are absent or oversized")
    try:
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EngineFactError("invalid external account statement JSON") from exc
    if type(doc) is not dict or set(doc) != REQUIRED_FIELDS or type(doc["schema_version"]) is not int:
        raise EngineFactError("invalid external account statement schema")
    if doc["schema_version"] != 1 or doc["account_id"] != expected_account_id:
        raise EngineFactError("foreign account or unsupported statement schema")
    _identity(doc["account_id"], "external statement account")
    record_id = _identity(doc["record_id"], "external statement record")
    if doc["source_kind"] != "BROKER_ACCOUNT_STATEMENT" or doc["currency"] != "USD":
        raise EngineFactError("only explicitly reported USD cash statements are supported")
    if type(doc["as_of_ns"]) is not int or doc["as_of_ns"] <= 0:
        raise EngineFactError("external statement timestamp is invalid")
    if doc["complete_instrument_scope"] is not True:
        raise EngineFactError("partial account holdings cannot establish arithmetic parity")
    if type(doc["positions"]) is not list or len(doc["positions"]) > 2048:
        raise EngineFactError("external account positions must be a bounded list")
    positions: dict[str, Decimal] = {}
    for item in doc["positions"]:
        if type(item) is not dict or set(item) != POSITION_FIELDS:
            raise EngineFactError("external position has missing or unexpected fields")
        symbol = item["symbol"]
        if type(symbol) is not str or not SYMBOL.fullmatch(symbol) or symbol in positions:
            raise EngineFactError("duplicate or invalid external instrument identity")
        positions[symbol] = _decimal_string(item["quantity"], "external position")
    return ExternalAccountStatement(
        account_id=doc["account_id"],
        source_ref=f"{source_namespace}:{record_id}",
        as_of_ns=doc["as_of_ns"],
        cash=_decimal_string(doc["cash"], "external cash"),
        positions=tuple(sorted(positions.items())),
        complete_instrument_scope=True,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def reconcile_external_account(book: EngineEconomicBook, statement: ExternalAccountStatement) -> ExternalAccountParity:
    """Compare three independent arithmetic views without manufacturing events.

    The journal and rational projector use the same economic facts, whereas
    the account statement is separate input. No authorization or source
    authentication may be inferred from equality of these quantities.
    """
    if type(book) is not EngineEconomicBook or type(statement) is not ExternalAccountStatement:
        raise EngineFactError("typed economic book and external statement are required")
    book.audit()
    if statement.account_id != book.account_id or not statement.complete_instrument_scope:
        raise EngineFactError("foreign or incomplete external account")
    if type(statement.as_of_ns) is not int or statement.as_of_ns <= 0:
        raise EngineFactError("invalid external statement observation time")
    if book.events and statement.as_of_ns < book.events[-1].timestamp_ns:
        raise EngineFactError("stale external account statement")
    if type(statement.positions) is not tuple or any(
        type(symbol) is not str or not SYMBOL.fullmatch(symbol) or type(quantity) is not Decimal
        for symbol, quantity in statement.positions
    ):
        raise EngineFactError("untyped external account holdings")
    observed: dict[str, Decimal] = {}
    for symbol, quantity in statement.positions:
        if symbol in observed:
            raise EngineFactError("duplicate external instrument identity")
        try:
            observed[symbol] = exact(quantity)
        except EconomicLedgerError as exc:
            raise EngineFactError("invalid external position precision or range") from exc
    if type(statement.cash) is not Decimal:
        raise EngineFactError("external statement cash is not exact Decimal")
    try:
        raw_cash = exact(statement.cash)
    except EconomicLedgerError as exc:
        raise EngineFactError("invalid external cash precision or range") from exc
    mathematical = compare_projection(book)
    independent = project_account(book)
    journal = book.ledger
    known_symbols = set(observed) | {lot.symbol for lot in journal.lots} | dict(independent.positions).keys()
    diffs = tuple(
        (symbol, _calc(lambda symbol=symbol: observed.get(symbol, Decimal(0)) - journal.position(symbol)))
        for symbol in sorted(known_symbols)
    )
    return ExternalAccountParity(
        book.account_id,
        statement.source_ref,
        statement.source_sha256,
        book.evidence_head,
        _calc(lambda: raw_cash - journal.balance("cash:USD")),
        diffs,
        mathematical.matches,
    )
