"""Local operator authority: identity-bound leases and single-use order approvals.

Authentication is the operating-system account and a 0600 HMAC key, not remote
identity verification. Generated signal workers cannot access this directory.
"""

import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from lab.persistence import atomic_json, file_lock, read_json

MODES = ("autonomous", "supervised", "manual")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Authority:
    def __init__(self, runtime, identity):
        self.runtime = Path(runtime)
        self.binding = digest(identity)
        self.identity = identity
        self.path = self.runtime / "authority.json"
        self.key_path = self.runtime / "authority.key"
        self.db_path = self.runtime / "approvals.sqlite3"

    def _key(self, create=False):
        if create and not self.key_path.exists():
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
                f.flush()
                os.fsync(f.fileno())
        fd = os.open(self.key_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            stat = os.fstat(f.fileno())
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                raise ValueError("authority key must be owned by this OS user and mode 0600")
            key = f.read()
        if len(key) != 32:
            raise ValueError("invalid authority key")
        return key

    def sign(self, payload):
        return hmac.new(self._key(), canonical(payload).encode(), hashlib.sha256).hexdigest()

    def read(self, *, allow_expired=False):
        rec = read_json(self.path)
        if (
            not isinstance(rec, dict)
            or not isinstance(rec.get("payload"), dict)
            or not isinstance(rec.get("hmac"), str)
        ):
            raise ValueError("malformed operator authority")
        payload = rec["payload"]
        if not hmac.compare_digest(rec["hmac"], self.sign(payload)) or payload["binding"] != self.binding:
            raise ValueError("authority signature or runtime identity differs")
        if payload["mode"] not in MODES:
            raise ValueError("invalid authority mode")
        if not allow_expired and not payload["issued"] <= time.time() < payload["expires"]:
            raise ValueError("operator authority expired or not yet valid")
        return payload

    def issue(self, mode, hours=24, targets=None):
        if mode not in MODES or type(hours) not in (float, int) or not math.isfinite(hours) or not 0 < hours <= 168:
            raise ValueError("mode must be autonomous, supervised or manual; lease must be 0..168 hours")
        targets = targets or {}
        if any(
            t not in self.identity["tickers"] or type(w) not in (float, int) or not math.isfinite(w)
            for t, w in targets.items()
        ):
            raise ValueError("manual targets must be finite weights for configured tickers")
        self.runtime.mkdir(parents=True, exist_ok=True)
        with file_lock(self.runtime / "authority.lock"):
            self._key(create=True)
            now = time.time()
            payload = {
                "version": 1,
                "binding": self.binding,
                "mode": mode,
                "issued": now,
                "expires": now + hours * 3600,
                "targets": targets,
                "nonce": os.urandom(16).hex(),
            }
            atomic_json(self.path, {"payload": payload, "hmac": self.sign(payload)})
        return payload

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS request (id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL, approval TEXT, used INTEGER NOT NULL DEFAULT 0)"
            )
            with db:
                yield db
        finally:
            db.close()

    def authorize(self, order, *, emergency=False, expected_nonce=None):
        # Reducing a reconciled position under an existing emergency control remains possible.
        if emergency:
            return True
        with file_lock(self.runtime / "authority.lock"):
            mandate = self.read()
            if expected_nonce is not None and mandate["nonce"] != expected_nonce:
                return False
            if mandate["mode"] in ("autonomous", "manual"):
                return True
            payload = {"binding": self.binding, "mandate": mandate["nonce"], "order": order}
            ident = digest(payload)
            now = time.time()
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM request WHERE expires<=?", (now,))
                row = db.execute("SELECT payload,expires,approval,used FROM request WHERE id=?", (ident,)).fetchone()
                if row is None:
                    if db.execute("SELECT COUNT(*) FROM request").fetchone()[0] >= 1000:
                        raise ValueError("approval queue capacity reached")
                    db.execute(
                        "INSERT INTO request(id,payload,expires) VALUES(?,?,?)",
                        (ident, canonical(payload), min(mandate["expires"], now + 3600)),
                    )
                    return False
                expected = self.sign({"request": ident, "expires": row[1]})
                if (
                    row[0] != canonical(payload)
                    or row[3]
                    or row[2] is None
                    or not hmac.compare_digest(row[2], expected)
                ):
                    return False
                db.execute("UPDATE request SET used=1 WHERE id=?", (ident,))
                return True

    def requests(self):
        with self.database() as db:
            return [
                {"id": k, "order": json.loads(p)["order"], "expires": e, "approved": a is not None}
                for k, p, e, a in db.execute(
                    "SELECT id,payload,expires,approval FROM request WHERE used=0 AND expires>? ORDER BY expires",
                    (time.time(),),
                )
            ]

    def approve(self, ident):
        with file_lock(self.runtime / "authority.lock"):
            mandate = self.read()
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT payload,expires,used FROM request WHERE id=?", (ident,)).fetchone()
                if row is None or row[2] or row[1] <= time.time():
                    raise ValueError("unknown, consumed or expired order proposal")
                payload = json.loads(row[0])
                if (
                    digest(payload) != ident
                    or payload["binding"] != self.binding
                    or payload["mandate"] != mandate["nonce"]
                    or mandate["mode"] != "supervised"
                ):
                    raise ValueError("order proposal no longer belongs to the active supervised mandate")
                db.execute(
                    "UPDATE request SET approval=? WHERE id=?",
                    (self.sign({"request": ident, "expires": row[1]}), ident),
                )
