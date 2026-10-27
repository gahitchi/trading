"""F1.3 broker-neutral cash-statement intake for a research-only engine.

This parser binds the exact bytes of an externally supplied activity record
to an F1 CashFact. It cannot authenticate the issuer, an economic entitlement,
or the fact of settlement. Never silently derive a deposit from cash drift.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from lab.engine_events import CashFact, CashInLieuFact, EngineEconomicBook, EngineFactError, _identity

REQUIRED = frozenset(
    {
        "schema_version",
        "account_id",
        "event_id",
        "record_id",
        "timestamp_ns",
        "kind",
        "source_kind",
        "settlement_status",
        "currency",
        "amount",
    }
)
MAX_RECORD_BYTES = 64 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EngineFactError("duplicate evidence-document field")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise EngineFactError("nonfinite values are invalid evidence")


def parse_settled_cash_record(
    raw: bytes,
    *,
    source_namespace: str,
    expected_account_id: str,
) -> CashFact:
    """Parse exactly one externally held, claimed-settled USD record.

    The raw document digest is recorded, but no signature or origin
    authentication is inferred from the source name or the content.
    """
    _identity(source_namespace, "statement source namespace")
    _identity(expected_account_id, "statement account")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise EngineFactError("raw statement must be a nonempty bounded byte document")
    try:
        record = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EngineFactError("statement is not valid UTF-8 JSON") from exc
    if type(record) is not dict or set(record) != REQUIRED:
        raise EngineFactError("cash statement fields are incomplete or unexpected")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise EngineFactError("unsupported statement schema")
    if record["account_id"] != expected_account_id or type(record["account_id"]) is not str:
        raise EngineFactError("statement belongs to a different account")
    event_id = _identity(record["event_id"], "statement event")
    record_id = _identity(record["record_id"], "statement record")
    time_ns = record["timestamp_ns"]
    if type(time_ns) is not int or time_ns <= 0:
        raise EngineFactError("statement requires a positive native nanosecond timestamp")
    if record["currency"] != "USD" or type(record["currency"]) is not str:
        raise EngineFactError("only USD settlement evidence is currently supported")
    amount = record["amount"]
    if type(amount) is not str:
        raise EngineFactError("statement amount must be an exact decimal string")
    try:
        decimal_amount = Decimal(amount)
    except InvalidOperation as exc:
        raise EngineFactError("invalid exact cash amount") from exc
    if (
        type(record["kind"]) is not str
        or type(record["source_kind"]) is not str
        or type(record["settlement_status"]) is not str
    ):
        raise EngineFactError("cash classification must be textual")
    return CashFact(
        event_id=event_id,
        source_ref=f"{source_namespace}:{record_id}",
        timestamp_ns=time_ns,
        kind=record["kind"],
        amount=decimal_amount,
        source_kind=record["source_kind"],
        settlement_status=record["settlement_status"],
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def append_settled_cash_record(
    book: EngineEconomicBook,
    raw: bytes,
    *,
    source_namespace: str,
) -> EngineEconomicBook:
    """Ingest a typed claimed-settled record; underlying ledger validates it.

    No broker session, trade fabrication, implicit interest accrual,
    automatic promotion from PENDING, or execution capability is provided.
    """
    if type(book) is not EngineEconomicBook:
        raise EngineFactError("cash statement requires an engine-owned economic book")
    fact = parse_settled_cash_record(raw, source_namespace=source_namespace, expected_account_id=book.account_id)
    return book.append(fact)


CASH_IN_LIEU_FIELDS = frozenset(
    {
        "schema_version",
        "account_id",
        "event_id",
        "record_id",
        "timestamp_ns",
        "kind",
        "source_kind",
        "settlement_status",
        "currency",
        "symbol",
        "side",
        "quantity",
        "cash",
    }
)


def parse_settled_cash_in_lieu_record(raw: bytes, *, source_namespace: str, expected_account_id: str) -> CashInLieuFact:
    """Parse a fractional nontrade cash adjustment with exact original bytes.

    This is evidence *custody*, not broker authentication; it cannot
    establish the position entitlement without a source-of-truth audit.
    """
    _identity(source_namespace, "statement source namespace")
    _identity(expected_account_id, "statement account")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise EngineFactError("cash-in-lieu source must be bounded original bytes")
    try:
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EngineFactError("invalid cash-in-lieu source document") from exc
    if type(doc) is not dict or set(doc) != CASH_IN_LIEU_FIELDS:
        raise EngineFactError("cash-in-lieu source keys incomplete or ambiguous")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise EngineFactError("unsupported cash-in-lieu schema")
    if type(doc["account_id"]) is not str or doc["account_id"] != expected_account_id:
        raise EngineFactError("foreign cash-in-lieu account")
    if doc["kind"] != "CASH_IN_LIEU" or type(doc["kind"]) is not str:
        raise EngineFactError("unsupported corporate-action settlement")
    if type(doc["currency"]) is not str or doc["currency"] != "USD":
        raise EngineFactError("cash-in-lieu is USD only")
    if type(doc["timestamp_ns"]) is not int or doc["timestamp_ns"] <= 0:
        raise EngineFactError("cash-in-lieu timestamp must be integer nanoseconds")
    if type(doc["side"]) is not str or type(doc["symbol"]) is not str:
        raise EngineFactError("cash-in-lieu instrument and side are required")
    if type(doc["quantity"]) is not str or type(doc["cash"]) is not str:
        raise EngineFactError("cash-in-lieu decimals must be exact source strings")
    if type(doc["source_kind"]) is not str or type(doc["settlement_status"]) is not str:
        raise EngineFactError("cash-in-lieu classification is invalid")
    try:
        qty = Decimal(doc["quantity"])
        cash = Decimal(doc["cash"])
    except InvalidOperation as exc:
        raise EngineFactError("invalid cash-in-lieu decimal strings") from exc
    return CashInLieuFact(
        event_id=_identity(doc["event_id"], "corporate-action event identity"),
        source_ref=f"{source_namespace}:{_identity(doc['record_id'], 'corporate-action record identity')}",
        timestamp_ns=doc["timestamp_ns"],
        symbol=doc["symbol"],
        side=doc["side"],
        quantity=qty,
        cash=cash,
        source_kind=doc["source_kind"],
        settlement_status=doc["settlement_status"],
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
