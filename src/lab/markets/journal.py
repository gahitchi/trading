"""SQLite-backed event journal for the future broker-neutral execution service.

A journal row is committed before the caller treats an action as durable. The
chain detects accidental modification or storage corruption, not malicious
rewriting by an attacker with filesystem access. NO orders are routed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

_ALLOWED_KINDS = {
    "intent",
    "submitted",
    "ack",
    "fill",
    "partial",
    "cancel",
    "reject",
    "unknown",
    "mark",
    "trade",
    "funding",
    "halt",
}
_GENESIS = "0" * 64


class JournalError(ValueError):
    pass


def _validate_payload(value) -> None:
    if value is None or type(value) in (str, int, bool):
        return
    if isinstance(value, list):
        for entry in value:
            _validate_payload(entry)
        return
    if isinstance(value, dict) and all(type(k) is str for k in value):
        for entry in value.values():
            _validate_payload(entry)
        return
    raise JournalError("journal payload must be JSON primitives, no floats or Decimal; serialize money as strings")


def _digest(previous: str, event_id: str, kind: str, timestamp_ns: int, payload: str) -> str:
    msg = json.dumps([previous, event_id, kind, timestamp_ns, payload], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(msg.encode("utf-8")).hexdigest()


class EventJournal:
    """Transactional single-stream event append with idempotent event IDs.

    Writers serialize through SQLite BEGIN IMMEDIATE; multi-host leases, broker
    transaction IDs and state reconstruction are separately required before D3.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.is_symlink():
            raise JournalError("refusing to open journal through a symbolic link")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        if os.name == "posix":
            self.path.chmod(0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
            "event_id TEXT NOT NULL UNIQUE,"
            "kind TEXT NOT NULL,"
            "timestamp_ns INTEGER NOT NULL,"
            "payload TEXT NOT NULL,"
            "previous_digest TEXT NOT NULL,"
            "digest TEXT NOT NULL)"
        )
        status = self._db.execute("PRAGMA quick_check").fetchone()
        if not status or status[0] != "ok":
            raise JournalError("SQLite journal integrity check failed")
        self.verify()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> EventJournal:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def append(self, event_id: str, kind: str, timestamp_ns: int, payload: dict) -> int:
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", event_id):
            raise JournalError("invalid event ID")
        if kind not in _ALLOWED_KINDS:
            raise JournalError("unknown event kind")
        if type(timestamp_ns) is not int or timestamp_ns < 0 or timestamp_ns >= 2**63:
            raise JournalError("invalid event timestamp")
        if not isinstance(payload, dict):
            raise JournalError("event payload must be an object")
        _validate_payload(payload)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._db.execute(
                "SELECT sequence, kind, timestamp_ns, payload FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing[1:] != (kind, timestamp_ns, canonical):
                    raise JournalError("idempotency ID reused with different execution evidence")
                self._db.execute("COMMIT")
                return int(existing[0])
            row = self._db.execute("SELECT digest FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
            previous = row[0] if row else _GENESIS
            digest = _digest(previous, event_id, kind, timestamp_ns, canonical)
            cursor = self._db.execute(
                "INSERT INTO events (event_id,kind,timestamp_ns,payload,previous_digest,digest) VALUES(?,?,?,?,?,?)",
                (event_id, kind, timestamp_ns, canonical, previous, digest),
            )
            self._db.execute("COMMIT")
            return int(cursor.lastrowid)
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def read_all(self) -> tuple[dict, ...]:
        rows = self._db.execute(
            "SELECT sequence,event_id,kind,timestamp_ns,payload,previous_digest,digest FROM events ORDER BY sequence"
        ).fetchall()
        return tuple(
            {
                "sequence": seq,
                "event_id": event_id,
                "kind": kind,
                "timestamp_ns": timestamp,
                "payload": json.loads(payload),
                "previous_digest": prev,
                "digest": digest,
            }
            for seq, event_id, kind, timestamp, payload, prev, digest in rows
        )

    def verify(self) -> None:
        prev = _GENESIS
        for event in self.read_all():
            payload = json.dumps(event["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if event["previous_digest"] != prev:
                raise JournalError("journal chain discontinuity")
            observed = _digest(prev, event["event_id"], event["kind"], event["timestamp_ns"], payload)
            if observed != event["digest"]:
                raise JournalError("journal event digest mismatch")
            prev = observed
