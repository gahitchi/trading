"""Transactional execution snapshot and indexed, non-expiring order/trade history.

Only active intents reside in memory. History is disk bounded (2 GiB by default);
capacity exhaustion fails closed rather than forgetting replay protection.
ledger.json is a diagnostic projection; ledger.sqlite3 is authoritative once created.
"""

import json
import sqlite3
import time
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path

from lab.execution_state import validate_ledger
from lab.persistence import atomic_json, read_json


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


class History(MutableMapping):
    def __init__(self, store, bucket):
        self.store, self.bucket = store, bucket
        self.dirty = {}

    def __getitem__(self, key):
        if key in self.dirty:
            return self.dirty[key]
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM history WHERE bucket=? AND id=?", (self.bucket, key)).fetchone()
        if row is None:
            raise KeyError(key)
        value = json.loads(row[0])
        if self.bucket == "applied":
            validate_ledger({"cash": 0, "applied_trades": [[key, *value]]})
        else:
            validate_ledger({"cash": 0, self.bucket: {key: value}})
        return value

    def __setitem__(self, key, value):
        self.dirty[key] = value

    def __delitem__(self, key):
        raise ValueError("execution history must not be erased")

    def __iter__(self):
        yield from self.dirty
        with self.store.connect() as db:
            for (key,) in db.execute("SELECT id FROM history WHERE bucket=?", (self.bucket,)):
                if key not in self.dirty:
                    yield key

    def __len__(self):
        with self.store.connect() as db:
            n = db.execute("SELECT COUNT(*) FROM history WHERE bucket=?", (self.bucket,)).fetchone()[0]
            return n + sum(
                db.execute("SELECT 1 FROM history WHERE bucket=? AND id=?", (self.bucket, k)).fetchone() is None
                for k in self.dirty
            )

    def __bool__(self):
        if self.dirty:
            return True
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM history WHERE bucket=? LIMIT 1", (self.bucket,)).fetchone() is not None

    def order_has_fill(self, coid):
        if any(v[1] == coid for v in self.dirty.values()):
            return True
        with self.store.connect() as db:
            return (
                db.execute("SELECT 1 FROM history WHERE bucket='applied' AND coid=? LIMIT 1", (coid,)).fetchone()
                is not None
            )


class ExecutionStore:
    def __init__(self, projection, *, max_bytes=2 * 1024**3):
        self.projection = Path(projection)
        self.path = self.projection.with_suffix(".sqlite3")
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        if not existed and not self.projection.exists() and (self.path.parent / "heartbeat.json").exists():
            raise ValueError("execution state missing from previously started runtime; restore complete backup")
        legacy = read_json(self.projection) if not existed and self.projection.exists() else None
        if legacy is not None:
            if legacy.get("authoritative_store"):
                raise ValueError("execution database missing; restore the complete runtime backup")
            validate_ledger(legacy)
        self.maps = {b: History(self, b) for b in ("terminal", "disposed", "applied")}
        with self.connect() as db:
            if existed:
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("unknown execution database version")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("execution database failed integrity check")
            else:
                db.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE history (bucket TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, coid TEXT, PRIMARY KEY(bucket,id)) WITHOUT ROWID"
                )
                db.execute("CREATE INDEX history_order ON history(bucket,coid)")
                db.execute("PRAGMA user_version=1")
        if existed and self.load() is None and self.projection.exists():
            legacy = read_json(self.projection)
            if legacy.get("authoritative_store"):
                raise ValueError("execution snapshot missing; restore complete runtime backup")
            validate_ledger(legacy)
        if legacy is not None:
            for b in ("terminal", "disposed"):
                self.maps[b].update(legacy.pop(b, {}))
            for entry in legacy.pop("applied_trades", []):
                tid, ts, coid = (entry, time.time_ns(), None) if isinstance(entry, str) else entry
                self.maps["applied"][tid] = [ts, coid]
            self.save(legacy)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            # DELETE journal keeps cold backups simple. A transaction is never split across
            # snapshot accounting and deduplication records. Page cap is set on every connection.
            page = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={max(8, self.max_bytes // page)}")
            with db:
                yield db
        finally:
            db.close()

    def load(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM snapshot WHERE id=1").fetchone()
        if row is None:
            return None
        state = json.loads(row[0])
        validate_ledger(state)
        return state

    def save(self, state):
        state = {**state, "version": 2, "authoritative_store": self.path.name}
        validate_ledger(state)
        for bucket, records in self.maps.items():
            if bucket == "applied":
                validate_ledger({"cash": 0, "applied_trades": [[k, *v] for k, v in records.dirty.items()]})
            else:
                validate_ledger({"cash": 0, bucket: records.dirty})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for bucket, records in self.maps.items():
                db.executemany(
                    "INSERT INTO history VALUES(?,?,?,?) ON CONFLICT(bucket,id) DO UPDATE SET value=excluded.value,coid=excluded.coid",
                    ((bucket, k, encode(v), v[1] if bucket == "applied" else None) for k, v in records.dirty.items()),
                )
            db.execute(
                "INSERT INTO snapshot VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value", (encode(state),)
            )
        for records in self.maps.values():
            records.dirty.clear()
        atomic_json(self.projection, state)

    def export(self):
        """Explicit audit export; may be large. Never used by the execution loop."""
        state = self.load() or {}
        for b in ("terminal", "disposed"):
            state[b] = dict(self.maps[b])
        state["applied_trades"] = [[k, *v] for k, v in self.maps["applied"].items()]
        return state
