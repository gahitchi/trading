"""F1.2 broker-neutral claim parser for one *executed* fill report.

A broker adapter must provide the original report bytes. This parser checks
content and exact arithmetic, not account login, issuer signature, custody
guarantees or order permissions. Every partial execution is its own event.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from lab.economic_ledger import EconomicLedgerError, exact
from lab.engine_events import BrokerFillFact, EngineFactError, _identity
from lab.settlement_input import MAX_RECORD_BYTES, _reject_nonfinite, _unique_object

REQUIRED = frozenset(
    {
        "schema_version",
        "account_id",
        "event_id",
        "record_id",
        "timestamp_ns",
        "kind",
        "status",
        "source_kind",
        "broker_order_id",
        "symbol",
        "side",
        "quote_currency",
        "commission_currency",
        "quantity",
        "price",
        "commission",
    }
)


def _decimal(text: object, description: str, *, positive: bool) -> Decimal:
    if type(text) is not str or len(text) > 80:
        raise EngineFactError(f"{description} must use a bounded exact decimal string")
    try:
        return exact(Decimal(text), positive=positive, nonnegative=not positive)
    except (InvalidOperation, EconomicLedgerError) as exc:
        raise EngineFactError(f"invalid broker execution {description}") from exc


def parse_broker_fill_record(
    raw: bytes, *, source_namespace: str, expected_account_id: str
) -> BrokerFillFact:
    """Reject anything except a fully identified USD execution report."""
    _identity(source_namespace, "execution source namespace")
    _identity(expected_account_id, "expected broker account")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RECORD_BYTES:
        raise EngineFactError("missing or oversized execution report bytes")
    try:
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EngineFactError("invalid execution report JSON") from exc
    if type(doc) is not dict or set(doc) != REQUIRED:
        raise EngineFactError("execution report keys are ambiguous or incomplete")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise EngineFactError("unsupported execution report schema")
    if type(doc["account_id"]) is not str or doc["account_id"] != expected_account_id:
        raise EngineFactError("execution report is from another account")
    if (
        doc["kind"] != "BROKER_FILL"
        or doc["status"] != "EXECUTED"
        or doc["source_kind"] != "BROKER_EXECUTION_REPORT"
    ):
        raise EngineFactError("execution report is not a confirmed fill claim")
    if doc["quote_currency"] != "USD" or doc["commission_currency"] != "USD":
        raise EngineFactError("USD quote and USD commission must be explicit")
    if type(doc["timestamp_ns"]) is not int or doc["timestamp_ns"] <= 0:
        raise EngineFactError("invalid execution report timestamp")
    if type(doc["side"]) is not str or doc["side"] not in ("BUY", "SELL"):
        raise EngineFactError("invalid execution report side")
    from lab.economic_ledger import SYMBOL

    symbol = doc["symbol"]
    if type(symbol) is not str or not SYMBOL.fullmatch(symbol):
        raise EngineFactError("invalid execution instrument")
    return BrokerFillFact(
        event_id=_identity(doc["event_id"], "execution event identity"),
        source_ref=f"{source_namespace}:{_identity(doc['record_id'], 'execution record identity')}",
        timestamp_ns=doc["timestamp_ns"],
        broker_order_id=_identity(doc["broker_order_id"], "execution order identity"),
        symbol=symbol,
        side=doc["side"],
        quantity=_decimal(doc["quantity"], "quantity", positive=True),
        price=_decimal(doc["price"], "price", positive=True),
        commission=_decimal(doc["commission"], "commission", positive=False),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
