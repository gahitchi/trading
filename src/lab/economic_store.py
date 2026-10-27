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
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from lab.economic_ledger import EconomicLedger
from lab.engine_events import (
    CashFact,
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
DECIMAL_FIELDS = frozenset({"quantity", "price", "commission", "amount"})
TYPES: dict[str, type] = {"FILL": FillFact, "CASH": CashFact, "SPLIT": SplitFact}


class EconomicStoreError(EngineFactError):
    """Stored account state or durable transaction could not be verified."""


def _encode(fact: EconomicFact) -> bytes:
    kind = "FILL" if type(fact) is FillFact else "CASH" if type(fact) is CashFact else "SPLIT"
    if type(fact) not in (FillFact, CashFact, SplitFact):
        raise EconomicStoreError("unexpected economic fact type")
    record = {"schema": SCHEMA, "type": kind}
    for name in (item.name for item in fields(fact)):
        value = getattr(fact, name)
        if name in DECIMAL_FIELDS:
            if type(value) is not Decimal:
                raise EconomicStoreError("only exact Decimal economic payloads may be persisted")
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


class EconomicEventStore:
    """Single USD-account append-only event journal, independently replayed.

    Only explicit CALLER-supplied immutable input events are accepted. Source
    bytes are mandatory for cash facts; this validates identity not origin.
    WAL commit does not constitute a broker settlement confirmation.
    """

    def __init__(self, path: Path, account_id: str):
        self.path = Path(path)
        self.account_id = _identity(account_id, "durable economic account")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
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
            row = db.execute("SELECT schema_version, account_id FROM identity WHERE singleton=1").fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO identity(singleton,schema_version,account_id) VALUES(1,?,?)",
                    (SCHEMA, self.account_id),
                )
            elif row != (SCHEMA, self.account_id):
                raise EconomicStoreError("durable account or schema identity changed")

    def _connect(self) -> sqlite3.Connection:
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
            if type(fact) is CashFact:
                if type(source_bytes) is not bytes or not 0 < len(source_bytes) <= MAX_SOURCE_BYTES:
                    raise EconomicStoreError("missing or invalid original settlement evidence")
                if hashlib.sha256(source_bytes).hexdigest() != fact.source_sha256:
                    raise EconomicStoreError("original settlement evidence does not match its digest")
            elif source_bytes is not None:
                raise EconomicStoreError("unsupported extra source bytes for noncash event")
            book = book.append(fact)
            journal_head = book.ledger.journal[-1].sha256
            if book.evidence_head != evidence_head or journal_head != ledger_head:
                raise EconomicStoreError("tampered or reordered economic event custody chain")
        book.audit()
        return book

    def load(self) -> EngineEconomicBook:
        with self._connect() as db:
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
        payload = _encode(fact)
        if len(payload) > MAX_EVENT_BYTES:
            raise EconomicStoreError("oversized economic fact")
        if type(fact) is CashFact:
            if type(source_bytes) is not bytes or not 0 < len(source_bytes) <= MAX_SOURCE_BYTES:
                raise EconomicStoreError("cash event requires original source bytes")
            if hashlib.sha256(source_bytes).hexdigest() != fact.source_sha256:
                raise EconomicStoreError("cash event source digest does not match supplied bytes")
        elif source_bytes is not None:
            raise EconomicStoreError("noncash source bytes have no qualified custody policy")
        if expected_head is not None and (type(expected_head) is not str or len(expected_head) != 64):
            raise EconomicStoreError("invalid expected custody fingerprint")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            book = self._read_locked(db)
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
