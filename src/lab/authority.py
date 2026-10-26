"""Local operator authority: identity-bound mandates and single-use order approvals.

An autonomous mandate is standing by default (no expiry): unattended, continuous trading is the base
(Gabriel, C003). A time limit is opt-in (`--authority-hours N`, at most 168), and supervised and manual
mandates are always leases. Any mandate is revoked at once by issuing another, or stopped by the kill switch.

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


def _hex(value, size):
    return isinstance(value, str) and len(value) == size and all(c in "0123456789abcdef" for c in value)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


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
        if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict) or not _hex(rec.get("hmac"), 64):
            raise ValueError("malformed operator authority")
        payload = rec["payload"]
        if payload.get("mode") == "autonomous":
            self._check_unattended_evidence()
        if not hmac.compare_digest(rec["hmac"], self.sign(payload)) or payload.get("binding") != self.binding:
            raise ValueError("authority signature or runtime identity differs")
        if (
            type(payload.get("version")) is not int
            or payload["version"] != 1
            or payload.get("mode") not in MODES
            or not _hex(payload.get("nonce"), 32)
            or not _finite(payload.get("issued"))
            or not self._valid_expiry(payload)
            or not self._valid_targets(payload.get("targets"))
        ):
            raise ValueError("invalid operator authority schema")
        if not allow_expired and not payload["issued"] <= time.time() < (payload["expires"] or math.inf):
            raise ValueError("operator authority expired or not yet valid")
        return payload

    @staticmethod
    def _valid_expiry(payload):
        expires = payload.get("expires")
        if expires is None:  # a standing mandate: autonomous only
            return payload.get("mode") == "autonomous"
        return _finite(expires) and 0 < expires - payload["issued"] <= 168 * 3600

    def _valid_targets(self, targets):
        return isinstance(targets, dict) and all(
            t in self.identity["tickers"] and _finite(w) for t, w in targets.items()
        )

    def _check_unattended_evidence(self):
        # Lightweight unit-test authorities may have no deployment context;
        # every real paper identity includes a deployment and its evidence.
        if self.identity.get("deployment") is None:
            return
        if not self.identity.get("autonomous_qualified", False):
            raise ValueError("unattended trading requires independently qualified strategy evidence")
        location = self.identity.get("autonomous_evidence_path")
        expected = self.identity.get("autonomous_evidence_sha256")
        if not isinstance(location, str) or not _hex(expected, 64):
            raise ValueError("unattended trading is missing approval manifest binding")
        try:
            fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as file:
                content = file.read(1_048_577)
            if len(content) > 1_048_576 or hashlib.sha256(content).hexdigest() != expected:
                raise ValueError("unattended trading approval evidence changed")
        except OSError as exc:
            raise ValueError("unattended trading approval evidence is unavailable") from exc

    def issue(self, mode, hours=None, targets=None):
        """hours None or 0: a standing autonomous mandate (no expiry). Supervised and manual mandates are leases
        (default 24 hours). Any lease is at most 168 hours."""
        if mode not in MODES:
            raise ValueError("mode must be autonomous, supervised or manual")
        if mode == "autonomous":
            self._check_unattended_evidence()
        if hours in (None, 0) and mode != "autonomous":
            hours = 24
        standing = hours in (None, 0)
        if not standing and (not _finite(hours) or not 0 < hours <= 168):
            raise ValueError("a lease must be more than 0 and at most 168 hours (0 or none: standing, autonomous)")
        targets = {} if targets is None else targets
        if not self._valid_targets(targets):
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
                "expires": None if standing else now + hours * 3600,
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

    def _request(self, ident, row):
        raw, expires, approval, used = row
        if (
            not isinstance(raw, str)
            or not _finite(expires)
            or type(used) is not int
            or used not in (0, 1)
            or (approval is not None and not _hex(approval, 64))
        ):
            raise ValueError("malformed operator approval record")
        try:
            payload = json.loads(raw)
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("order"), dict)
                or payload.get("binding") != self.binding
                or not _hex(payload.get("mandate"), 32)
                or digest(payload) != ident
            ):
                raise ValueError("invalid proposal")
        except (ValueError, TypeError) as exc:
            raise ValueError("malformed operator approval payload") from exc
        return payload

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
                        (ident, canonical(payload), min(mandate["expires"] or math.inf, now + 3600)),
                    )
                    return False
                self._request(ident, row)
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
        with file_lock(self.runtime / "authority.lock"), self.database() as db:
            return [
                {
                    "id": k,
                    "order": self._request(k, (p, e, a, used))["order"],
                    "expires": e,
                    "approved": a is not None,
                }
                for k, p, e, a, used in db.execute(
                    "SELECT id,payload,expires,approval,used FROM request WHERE used=0 AND expires>? ORDER BY expires",
                    (time.time(),),
                )
            ]

    def approve(self, ident):
        with file_lock(self.runtime / "authority.lock"):
            mandate = self.read()
            with self.database() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT payload,expires,approval,used FROM request WHERE id=?", (ident,)).fetchone()
                payload = self._request(ident, row) if row is not None else None
                if row is None or row[3] or row[1] <= time.time():
                    raise ValueError("unknown, consumed or expired order proposal")
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
