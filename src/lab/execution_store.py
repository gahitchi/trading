"""Transactional execution snapshot and indexed, non-expiring order/trade history.

Only active intents reside in memory. History is disk bounded (2 GiB by default);
capacity exhaustion fails closed rather than forgetting replay protection.
ledger.json is a diagnostic projection; ledger.sqlite3 is authoritative once created.
"""

import json
import math
import sqlite3
import time
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path

from lab.execution_state import validate_ledger
from lab.persistence import atomic_json, read_json


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def decode(raw):
    """Reject malformed SQLite JSON before evaluating any monetary state.

    SQLite page-integrity checks cannot detect logically invalid JSON; Python's
    default decoder accepts NaN, infinite exponent floats and duplicate keys.
    """

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite SQLite execution JSON float")
        return result

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate SQLite execution JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"nonfinite SQLite execution JSON constant: {value}")

    return json.loads(raw, parse_float=finite_float, parse_constant=invalid_constant, object_pairs_hook=unique_object)


class History(MutableMapping):
    def __init__(self, store, bucket):
        self.store, self.bucket = store, bucket
        self.dirty = {}

    def __getitem__(self, key):
        if key in self.dirty:
            return self.dirty[key]
        with self.store.connect() as db:
            row = db.execute("SELECT value,coid FROM history WHERE bucket=? AND id=?", (self.bucket, key)).fetchone()
        if row is None:
            raise KeyError(key)
        value = decode(row[0])
        if self.bucket == "applied":
            if not isinstance(value, list):
                raise ValueError("invalid stored applied execution payload")
            validate_ledger({"cash": 0, "applied_trades": [[key, *value]]})
            if value[1] != row[1]:
                raise ValueError("execution history order index and recorded attribution differ")
        else:
            validate_ledger({"cash": 0, self.bucket: {key: value}})
            if row[1] is not None:
                raise ValueError("non-applied history cannot carry execution attribution")
        return value

    def __setitem__(self, key, value):
        # An execution ID is an immutable attribution key. In-memory
        # replacement must not erase a conflicting receipt before save().
        if self.bucket == "applied" and key in self.dirty and self.dirty[key] != value:
            raise ValueError("conflicting applied trade ID in pending execution history")
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
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (1, 2, 3):
                    raise ValueError("unknown execution database version")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("execution database failed integrity check")
                if version < 3:
                    db.execute("BEGIN IMMEDIATE")
                    if version == 1:
                        db.execute("ALTER TABLE snapshot ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
                    # Existing v1/v2 stores did not persist a separate 'ever
                    # committed' marker; migrate the state that survives.
                    db.execute(
                        "CREATE TABLE recovery_marker (id INTEGER PRIMARY KEY CHECK(id=1), "
                        "snapshot_seen INTEGER NOT NULL CHECK(snapshot_seen IN (0,1)))"
                    )
                    seen = db.execute("SELECT 1 FROM snapshot WHERE id=1").fetchone() is not None
                    db.execute("INSERT INTO recovery_marker VALUES(1,?)", (int(seen),))
                    db.execute("PRAGMA user_version=3")
            else:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE snapshot (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "value TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation >= 1))"
                )
                db.execute(
                    "CREATE TABLE history (bucket TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, coid TEXT, PRIMARY KEY(bucket,id)) WITHOUT ROWID"
                )
                db.execute("CREATE INDEX history_order ON history(bucket,coid)")
                db.execute(
                    "CREATE TABLE recovery_marker (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "snapshot_seen INTEGER NOT NULL CHECK(snapshot_seen IN (0,1)))"
                )
                db.execute("INSERT INTO recovery_marker VALUES(1,0)")
                db.execute("PRAGMA user_version=3")
            # Pin both value and monotonic generation. The persistent marker
            # independently distinguishes a never-saved DB from an erased
            # monetary snapshot, including after ledger.json is lost.
            row = db.execute("SELECT value,generation FROM snapshot WHERE id=1").fetchone()
            marker = db.execute("SELECT snapshot_seen FROM recovery_marker WHERE id=1").fetchone()
            if marker is None or type(marker[0]) is not int or marker[0] not in (0, 1):
                raise ValueError("missing or invalid execution recovery marker")
            self._snapshot_seen = bool(marker[0])
            if (row is None and self._snapshot_seen) or (row is not None and not self._snapshot_seen):
                raise ValueError("execution snapshot and durable recovery marker disagree")
            self._expected_snapshot = row[0] if row is not None else None
            self._expected_generation = row[1] if row is not None else 0
        if existed and self.load() is None:
            # An existing database without an authoritative monetary snapshot
            # cannot be treated as a new account, even if ledger.json and the
            # heartbeat were deleted. A legacy JSON import may resume only if
            # no history ever committed: importing it onto orphaned fills or
            # orders could silently invent new cash or omit executions.
            with self.connect() as db:
                if db.execute("SELECT 1 FROM history LIMIT 1").fetchone() is not None:
                    raise ValueError("execution snapshot missing with recorded history; restore complete backup")
            if not self.projection.exists():
                # A second writer can observe a genuinely new, never-saved DB.
                # Its own generation-CAS will reject any later competing save.
                legacy = None
            else:
                legacy = read_json(self.projection)
            if legacy is not None:
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
        state = decode(row[0])
        validate_ledger(state)
        if state.get("authoritative_store") != self.path.name:
            raise ValueError("execution snapshot/store binding differs")
        return state

    def save(self, state):
        state = {**state, "version": 3, "authoritative_store": self.path.name}
        validate_ledger(state)
        for bucket, records in self.maps.items():
            if bucket == "applied":
                validate_ledger({"cash": 0, "applied_trades": [[k, *v] for k, v in records.dirty.items()]})
            else:
                validate_ledger({"cash": 0, bucket: records.dirty})
        snapshot = encode(state)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value,generation FROM snapshot WHERE id=1").fetchone()
            current = row[0] if row is not None else None
            generation = row[1] if row is not None else 0
            if current != self._expected_snapshot or generation != self._expected_generation:
                raise ValueError("stale execution snapshot writer: authoritative state changed")
            for bucket, records in self.maps.items():
                if bucket == "applied":
                    for key, value in records.dirty.items():
                        raw = encode(value)
                        existing = db.execute(
                            "SELECT value FROM history WHERE bucket='applied' AND id=?", (key,)
                        ).fetchone()
                        if existing is not None and existing[0] != raw:
                            raise ValueError("conflicting applied trade ID in committed execution history")
                        if existing is None:
                            db.execute(
                                "INSERT INTO history(bucket,id,value,coid) VALUES(?,?,?,?)",
                                (bucket, key, raw, value[1]),
                            )
                else:
                    db.executemany(
                        "INSERT INTO history VALUES(?,?,?,?) "
                        "ON CONFLICT(bucket,id) DO UPDATE SET value=excluded.value,coid=excluded.coid",
                        ((bucket, k, encode(v), None) for k, v in records.dirty.items()),
                    )
            db.execute(
                "INSERT INTO snapshot(id,value,generation) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value,generation=excluded.generation",
                (snapshot, generation + 1),
            )
            db.execute("UPDATE recovery_marker SET snapshot_seen=1 WHERE id=1")
        # SQLite committed the new authoritative head. Advance the local
        # baseline BEFORE writing the best-effort JSON projection: a crash in
        # the latter cannot make a later save reuse a stale head.
        self._expected_snapshot = snapshot
        self._expected_generation = generation + 1
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
