"""F1.4 prototype: durable broker-neutral economic fact custody.

SQLite WAL + synchronous FULL with BEGIN IMMEDIATE makes per-event source
bytes, economic inputs and after-state digests atomic. All events are
reconstructed from persisted bytes on read. Neither a local SHA chain nor
SQLite can authenticate the external source or defeat a privileged DB rewrite.
No broker, Nautilus, live-order or execution authority is present.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from lab.economic_ledger import EconomicLedgerError, exact
from lab.engine_events import (
    SHA256,
    BrokerFillFact,
    CashFact,
    CashReversalFact,
    CashInLieuFact,
    EconomicFact,
    EngineEconomicBook,
    EngineFactError,
    FillFact,
    SplitFact,
    _identity,
)

SCHEMA = 1
MAX_EVENT_BYTES = 16_384
MAX_SOURCE_BYTES = 65_536
MAX_ORDER_OBSERVATIONS = 1024
DECIMAL_FIELDS = frozenset({"quantity", "price", "commission", "amount", "cash"})
TYPES: dict[str, type] = {
    "FILL": FillFact,
    "BROKER_FILL": BrokerFillFact,
    "CASH": CashFact,
    "CASH_REVERSAL": CashReversalFact,
    "SPLIT": SplitFact,
    "CASH_IN_LIEU": CashInLieuFact,
}


class EconomicStoreError(EngineFactError):
    """Stored account state or durable transaction could not be verified."""


@dataclass(frozen=True, slots=True)
class StoredOrderObservation:
    """Local custody receipt, not a broker-issued order status attestation."""

    source_ref: str
    source_sha256: str
    as_of_ns: int
    ledger_evidence_head: str
    events_replayed: int
    broker_fills_checked: int
    broker_source_authenticated: bool = False


def _encode(fact: EconomicFact) -> bytes:
    kind = (
        "FILL"
        if type(fact) is FillFact
        else "BROKER_FILL"
        if type(fact) is BrokerFillFact
        else "CASH"
        if type(fact) is CashFact
        else "CASH_REVERSAL"
        if type(fact) is CashReversalFact
        else "CASH_IN_LIEU"
        if type(fact) is CashInLieuFact
        else "SPLIT"
    )
    if type(fact) not in (FillFact, BrokerFillFact, CashFact, CashReversalFact, SplitFact, CashInLieuFact):
        raise EconomicStoreError("unexpected economic fact type")
    record = {"schema": SCHEMA, "type": kind}
    for name in (item.name for item in fields(fact)):
        value = getattr(fact, name)
        if name in DECIMAL_FIELDS:
            if type(value) is not Decimal:
                raise EconomicStoreError("only exact Decimal economic payloads may be persisted")
            try:
                value = exact(value)
            except EconomicLedgerError as exc:
                raise EconomicStoreError("economic payload exceeds bounded precision or scale") from exc
            record[name] = format(value, "f")
        else:
            record[name] = value
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _decode(payload: bytes) -> EconomicFact:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_EVENT_BYTES:
        raise EconomicStoreError("unbounded or empty persisted event payload")
    try:
        record = json.loads(payload.decode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise EconomicStoreError("corrupt event JSON") from exc
    if type(record) is not dict or type(record.get("type")) is not str or type(record.get("schema")) is not int:
        raise EconomicStoreError("invalid economic record structure")
    cls = TYPES.get(record["type"])
    if record["schema"] != SCHEMA or cls is None:
        raise EconomicStoreError("unknown event schema/kind")
    names = {item.name for item in fields(cls)}
    if set(record) != names | {"schema", "type"}:
        raise EconomicStoreError("economic payload fields are incomplete or unexpected")
    arguments: dict[str, Any] = {}
    for key in names:
        value = record[key]
        if key in DECIMAL_FIELDS:
            if type(value) is not str:
                raise EconomicStoreError("persisted economic decimals must be strings")
            try:
                arguments[key] = Decimal(value)
            except (ValueError, ArithmeticError) as exc:
                raise EconomicStoreError("corrupt persisted decimal value") from exc
        elif key in {"timestamp_ns", "numerator", "denominator"}:
            if type(value) is not int:
                raise EconomicStoreError("persisted quantity/timestamp must be integer")
            arguments[key] = value
        else:
            if type(value) is not str:
                raise EconomicStoreError("persisted identifiers must be strings")
            arguments[key] = value
    fact = cls(**arguments)
    if _encode(fact) != payload:
        raise EconomicStoreError("persisted event payload has noncanonical encoding")
    return fact


def _verify_source_binding(fact: EconomicFact, raw: bytes, account_id: str) -> None:
    """Reparse original bytes and prove the *claimed* fact is exactly that record.

    SHA256(raw) alone is insufficient: without this check a caller could
    attach genuine cash-statement bytes to fabricated financial fields.
    This is content integrity, never issuer authentication or finality.
    """
    from lab.fill_input import parse_broker_fill_record
    from lab.settlement_input import (
        _reject_nonfinite,
        _unique_object,
        parse_settled_cash_in_lieu_record,
        parse_settled_cash_record,
    )

    if type(fact) not in (CashFact, CashReversalFact, CashInLieuFact, BrokerFillFact):
        raise EconomicStoreError("source binding requires classified source fact")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise EconomicStoreError("missing or invalid original settlement bytes")
    if hashlib.sha256(raw).hexdigest() != fact.source_sha256:
        raise EconomicStoreError("source digest does not match original statement bytes")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
        if type(document) is not dict or type(document.get("record_id")) is not str:
            raise EconomicStoreError("settlement record is missing a typed source identifier")
        suffix = ":" + document["record_id"]
        if not fact.source_ref.endswith(suffix):
            raise EconomicStoreError("source reference does not match original record")
        namespace = _identity(fact.source_ref[: -len(suffix)], "source namespace")
        if type(fact) is CashReversalFact:
            from lab.settlement_input import parse_settled_cash_reversal_record

            parser = parse_settled_cash_reversal_record
        elif type(fact) is CashFact:
            parser = parse_settled_cash_record
        elif type(fact) is CashInLieuFact:
            parser = parse_settled_cash_in_lieu_record
        else:
            parser = parse_broker_fill_record
        observed = parser(raw, source_namespace=namespace, expected_account_id=account_id)
    except (UnicodeError, ValueError, TypeError, EngineFactError) as exc:
        raise EconomicStoreError("original statement cannot establish claimed economic source") from exc
    if observed != fact:
        raise EconomicStoreError("claimed economic fields do not match the original settlement record")


class EconomicEventStore:
    """Single USD-account append-only event journal, independently replayed.

    Only explicit CALLER-supplied immutable input events are accepted. Source
    bytes are mandatory for cash facts; this validates identity not origin.
    WAL commit does not constitute a broker settlement confirmation.
    """

    def __init__(self, path: Path, account_id: str, *, readonly: bool = False):
        self.path = Path(path)
        self.account_id = _identity(account_id, "durable economic account")
        if type(readonly) is not bool:
            raise EconomicStoreError("economic store access mode must be explicit bool")
        self.readonly = readonly
        if readonly:
            if not self.path.is_file():
                raise EconomicStoreError("read-only economic database does not exist")
            self.load()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS identity ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "schema_version INTEGER NOT NULL, account_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS economic_events ("
                "seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, "
                "source_ref TEXT NOT NULL UNIQUE, payload BLOB NOT NULL, "
                "source_bytes BLOB, evidence_head TEXT NOT NULL, "
                "ledger_head TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS external_order_observations ("
                "seq INTEGER PRIMARY KEY, source_ref TEXT NOT NULL UNIQUE, "
                "source_namespace TEXT NOT NULL, as_of_ns INTEGER NOT NULL, "
                "event_count INTEGER NOT NULL, evidence_head TEXT NOT NULL, "
                "source_sha256 TEXT NOT NULL, source_bytes BLOB NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS broker_order_events ("
                "seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, "
                "source_ref TEXT NOT NULL UNIQUE, broker_order_id TEXT NOT NULL, "
                "source_namespace TEXT NOT NULL, source_sha256 TEXT NOT NULL, "
                "source_bytes BLOB NOT NULL, fill_source_bytes BLOB, "
                "pre_economic_head TEXT NOT NULL, post_economic_head TEXT NOT NULL, "
                "order_chain TEXT NOT NULL)"
            )
            row = db.execute("SELECT schema_version, account_id FROM identity WHERE singleton=1").fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO identity(singleton,schema_version,account_id) VALUES(1,?,?)",
                    (SCHEMA, self.account_id),
                )
            elif row != (SCHEMA, self.account_id):
                raise EconomicStoreError("durable account or schema identity changed")

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            db = sqlite3.connect(f"{self.path.absolute().as_uri()}?mode=ro", uri=True, timeout=15, isolation_level=None)
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            return db
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        return db

    def _read_locked(self, db: sqlite3.Connection) -> EngineEconomicBook:
        identity = db.execute("SELECT schema_version, account_id FROM identity WHERE singleton=1").fetchall()
        if identity != [(SCHEMA, self.account_id)]:
            raise EconomicStoreError("missing, foreign or duplicated account identity")
        book = EngineEconomicBook(self.account_id)
        rows = db.execute(
            "SELECT seq,event_id,source_ref,payload,source_bytes,evidence_head,ledger_head "
            "FROM economic_events ORDER BY seq"
        )
        for count, row in enumerate(rows, 1):
            seq, event_id, source_ref, payload, source_bytes, evidence_head, ledger_head = row
            if seq != count or type(payload) is not bytes:
                raise EconomicStoreError("event sequencing or payload bytes are corrupted")
            fact = _decode(payload)
            if fact.event_id != event_id or fact.source_ref != source_ref:
                raise EconomicStoreError("persisted economic/source identity differs")
            if type(fact) in (CashFact, CashReversalFact, CashInLieuFact, BrokerFillFact):
                if type(source_bytes) is not bytes or not 0 < len(source_bytes) <= MAX_SOURCE_BYTES:
                    raise EconomicStoreError("missing or invalid original settlement evidence")
                _verify_source_binding(fact, source_bytes, self.account_id)
            elif source_bytes is not None:
                raise EconomicStoreError("unsupported extra source bytes for noncash event")
            book = book.append(fact)
            journal_head = book.ledger.journal[-1].sha256
            if book.evidence_head != evidence_head or journal_head != ledger_head:
                raise EconomicStoreError("tampered or reordered economic event custody chain")
        book.audit()
        return book

    def load(self) -> EngineEconomicBook:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            result = self._read_locked(db)
            db.execute("COMMIT")
            return result

    def append(
        self,
        fact: EconomicFact,
        *,
        source_bytes: bytes | None = None,
        expected_head: str | None = None,
    ) -> EngineEconomicBook:
        """Commit exactly one fact; return independently replayed account book.

        Idempotent same-event retry after an uncertain commit is supported.
        Expected head is an optimistic concurrency fence for *new* events.
        """
        if self.readonly:
            raise EconomicStoreError("read-only economic store cannot append financial events")
        payload = _encode(fact)
        if len(payload) > MAX_EVENT_BYTES:
            raise EconomicStoreError("oversized economic fact")
        if type(fact) in (CashFact, CashReversalFact, CashInLieuFact, BrokerFillFact):
            if type(source_bytes) is not bytes or not 0 < len(source_bytes) <= MAX_SOURCE_BYTES:
                raise EconomicStoreError("cash event requires original source bytes")
            _verify_source_binding(fact, source_bytes, self.account_id)
        elif source_bytes is not None:
            raise EconomicStoreError("noncash source bytes have no qualified custody policy")
        if expected_head is not None and (type(expected_head) is not str or len(expected_head) != 64):
            raise EconomicStoreError("invalid expected custody fingerprint")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            book = self._read_locked(db)
            if (
                type(fact) is BrokerFillFact
                and db.execute(
                    "SELECT 1 FROM broker_order_events WHERE broker_order_id=? LIMIT 1",
                    (fact.broker_order_id,),
                ).fetchone()
                is not None
            ):
                raise EconomicStoreError("tracked broker fills require atomic order lifecycle custody")
            existing = db.execute(
                "SELECT payload,source_bytes FROM economic_events WHERE event_id=?",
                (fact.event_id,),
            ).fetchone()
            if existing is not None:
                if existing != (payload, source_bytes):
                    raise EconomicStoreError("event identity reused with conflicting economic evidence")
                db.execute("COMMIT")
                return book
            if expected_head is not None and expected_head != book.evidence_head:
                raise EconomicStoreError("stale account custody fence")
            book = book.append(fact)
            db.execute(
                "INSERT INTO economic_events (seq,event_id,source_ref,payload,source_bytes,evidence_head,ledger_head)"
                "VALUES (?,?,?,?,?,?,?)",
                (
                    len(book.events),
                    fact.event_id,
                    fact.source_ref,
                    payload,
                    source_bytes,
                    book.evidence_head,
                    book.ledger.journal[-1].sha256,
                ),
            )
            db.execute("COMMIT")
            return book

    def append_cash_record(
        self, raw: bytes, *, source_namespace: str, expected_head: str | None = None
    ) -> EngineEconomicBook:
        """Validate the source record and atomically persist its exact bytes."""
        from lab.settlement_input import parse_settled_cash_record

        fact = parse_settled_cash_record(
            raw,
            source_namespace=source_namespace,
            expected_account_id=self.account_id,
        )
        return self.append(fact, source_bytes=raw, expected_head=expected_head)

    def append_cash_reversal_record(
        self, raw: bytes, *, source_namespace: str, expected_head: str | None = None
    ) -> EngineEconomicBook:
        """Apply only a sourced correction that reverses one original CashFact."""
        from lab.settlement_input import parse_settled_cash_reversal_record

        fact = parse_settled_cash_reversal_record(
            raw, source_namespace=source_namespace, expected_account_id=self.account_id
        )
        return self.append(fact, source_bytes=raw, expected_head=expected_head)

    def append_cash_in_lieu_record(
        self, raw: bytes, *, source_namespace: str, expected_head: str | None = None
    ) -> EngineEconomicBook:
        """Book fractional corporate-action cash through the same atomic custody."""
        from lab.settlement_input import parse_settled_cash_in_lieu_record

        fact = parse_settled_cash_in_lieu_record(
            raw,
            source_namespace=source_namespace,
            expected_account_id=self.account_id,
        )
        return self.append(fact, source_bytes=raw, expected_head=expected_head)

    def append_broker_fill_record(
        self, raw: bytes, *, source_namespace: str, expected_head: str | None = None
    ) -> EngineEconomicBook:
        """Atomically preserve raw broker execution claim and its exact economics.

        This still requires authenticated broker adapters and an order-state
        reconciliation path before use with supervised paper trading.
        """
        from lab.fill_input import parse_broker_fill_record

        fact = parse_broker_fill_record(raw, source_namespace=source_namespace, expected_account_id=self.account_id)
        return self.append(fact, source_bytes=raw, expected_head=expected_head)

    def capture_order_observation(
        self, raw: bytes, *, source_namespace: str, expected_head: str
    ) -> StoredOrderObservation:
        """Atomically archive complete external order bytes at an exact ledger head.

        No economic event, position, balance, order or risk decision is changed.
        Source content integrity is checked, NOT the issuer's authenticity.
        """
        from lab.order_observation import parse_external_order_statement, reconcile_external_orders

        if self.readonly:
            raise EconomicStoreError("read-only economic store cannot archive observations")
        _identity(source_namespace, "order observation source namespace")
        if type(expected_head) is not str or not SHA256.fullmatch(expected_head):
            raise EconomicStoreError("an exact economic journal head is required")
        report = parse_external_order_statement(
            raw, source_namespace=source_namespace, expected_account_id=self.account_id
        )
        if report.schema_version != 2:
            raise EconomicStoreError("durable order custody requires complete v2 receipt identities")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            book = self._read_locked(db)
            existing = db.execute(
                "SELECT source_namespace,as_of_ns,event_count,evidence_head,source_sha256,source_bytes "
                "FROM external_order_observations WHERE source_ref=?",
                (report.source_ref,),
            ).fetchone()
            if existing is not None:
                original = (
                    source_namespace,
                    report.as_of_ns,
                    existing[2],
                    expected_head,
                    report.source_sha256,
                    raw,
                )
                if existing != original:
                    raise EconomicStoreError("order observation identity reused with conflicting evidence")
                historical_count = existing[2]
                if type(historical_count) is not int or not 0 <= historical_count <= len(book.events):
                    raise EconomicStoreError("archived order retry has an invalid historical event count")
                historical = EngineEconomicBook(self.account_id)
                for fact in book.events[:historical_count]:
                    historical = historical.append(fact)
                if historical.evidence_head != expected_head:
                    raise EconomicStoreError("archived order retry has a corrupted historical ledger head")
                historical_parity = reconcile_external_orders(historical, report)
                if not historical_parity.matches or not historical_parity.receipt_scope_checked:
                    raise EconomicStoreError("archived order retry disagrees with historical economic facts")
                db.execute("COMMIT")
                return StoredOrderObservation(
                    report.source_ref,
                    report.source_sha256,
                    report.as_of_ns,
                    expected_head,
                    historical_count,
                    historical_parity.broker_fills_checked,
                )
            if expected_head != book.evidence_head:
                raise EconomicStoreError("stale economic head for external order custody")
            parity = reconcile_external_orders(book, report)
            if not parity.matches or not parity.receipt_scope_checked:
                raise EconomicStoreError("external order observation cannot establish complete receipt parity")
            count = db.execute("SELECT COUNT(*) FROM external_order_observations").fetchone()[0]
            if count >= MAX_ORDER_OBSERVATIONS:
                raise EconomicStoreError("bounded external order observation archive is full")
            db.execute(
                "INSERT INTO external_order_observations "
                "(seq,source_ref,source_namespace,as_of_ns,event_count,evidence_head,source_sha256,source_bytes) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    count + 1,
                    report.source_ref,
                    source_namespace,
                    report.as_of_ns,
                    len(book.events),
                    book.evidence_head,
                    report.source_sha256,
                    raw,
                ),
            )
            db.execute("COMMIT")
            return StoredOrderObservation(
                report.source_ref,
                report.source_sha256,
                report.as_of_ns,
                book.evidence_head,
                len(book.events),
                parity.broker_fills_checked,
            )

    def inspect_order_observations(self) -> tuple[StoredOrderObservation, ...]:
        """Read-only, full-byte replay of externally CLAIMED historical reports.

        Reconstruct only the original economic prefix for each report, never
        the later book, so observations do not become invalid merely because
        unrelated subsequent economic facts were appended.
        """
        from lab.order_observation import parse_external_order_statement, reconcile_external_orders

        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            book = self._read_locked(db)
            heads = (
                "0" * 64,
                *(row[0] for row in db.execute("SELECT evidence_head FROM economic_events ORDER BY seq")),
            )
            rows = db.execute(
                "SELECT seq,source_ref,source_namespace,as_of_ns,event_count,evidence_head,"
                "source_sha256,source_bytes FROM external_order_observations ORDER BY seq"
            ).fetchall()
            if len(rows) > MAX_ORDER_OBSERVATIONS:
                raise EconomicStoreError("external order observation archive exceeds its bound")
            results: list[StoredOrderObservation] = []
            for index, row in enumerate(rows, 1):
                seq, source_ref, namespace, as_of, n_events, head, digest, raw = row
                if (
                    type(seq) is not int
                    or seq != index
                    or type(n_events) is not int
                    or not 0 <= n_events <= len(book.events)
                    or type(head) is not str
                    or head != heads[n_events]
                    or type(raw) is not bytes
                ):
                    raise EconomicStoreError("order observation archive sequence or evidence head is corrupt")
                report = parse_external_order_statement(
                    raw, source_namespace=namespace, expected_account_id=self.account_id
                )
                if (
                    report.schema_version != 2
                    or report.source_ref != source_ref
                    or report.as_of_ns != as_of
                    or report.source_sha256 != digest
                ):
                    raise EconomicStoreError("archived order source bytes contradict stored identity or time")
                prefix = EngineEconomicBook(self.account_id)
                for fact in book.events[:n_events]:
                    prefix = prefix.append(fact)
                if prefix.evidence_head != head:
                    raise EconomicStoreError("archived order observation does not match economic prefix")
                parity = reconcile_external_orders(prefix, report)
                if not parity.matches or not parity.receipt_scope_checked:
                    raise EconomicStoreError("archived external orders disagree with their economic prefix")
                results.append(
                    StoredOrderObservation(
                        source_ref,
                        digest,
                        as_of,
                        head,
                        n_events,
                        parity.broker_fills_checked,
                    )
                )
            db.execute("COMMIT")
            return tuple(results)
