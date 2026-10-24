"""
Notifications. Messages are queued and sent from a background thread, so network
trouble never blocks the trading agents.

Telegram (free): create a bot with @BotFather, send it any message, then set
  LAB_TELEGRAM_TOKEN=<bot token>  LAB_TELEGRAM_CHAT_ID=<your chat id>
Without them, notifications only go to the log.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("lab.notify")


class Notifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        prefix: str = "trading-lab",
        spool_path: str | None = None,
        max_pending: int = 10000,
    ):
        self.token = token if token is not None else os.environ.get("LAB_TELEGRAM_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("LAB_TELEGRAM_CHAT_ID", "")
        self.prefix = prefix
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("notification capacity must be positive")
        self.max_pending = max_pending
        self.enqueue_error = False
        self.sent = 0
        self.failed = 0
        self._q: queue.Queue = queue.Queue(maxsize=500)
        self.spool_path = spool_path
        self._wake = threading.Event()
        if spool_path:
            Path(spool_path).parent.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                # Serialize migration with other notifier instances. AUTOINCREMENT prevents an old
                # worker acknowledgement deleting a new message which reused the last deleted ID.
                db.execute("BEGIN IMMEDIATE")
                schema = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='notification'"
                ).fetchone()
                if schema is not None and "AUTOINCREMENT" not in schema[0].upper():
                    db.execute("ALTER TABLE notification RENAME TO notification_legacy")
                db.execute("CREATE TABLE IF NOT EXISTS notification (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
                if schema is not None and "AUTOINCREMENT" not in schema[0].upper():
                    db.execute("INSERT INTO notification(id,text) SELECT id,text FROM notification_legacy")
                    db.execute("DROP TABLE notification_legacy")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS notification_time (id INTEGER PRIMARY KEY, created REAL NOT NULL)"
                )
                db.execute("INSERT OR IGNORE INTO notification_time SELECT id,0 FROM notification")
        self._worker = None
        if self.enabled:
            self._worker = threading.Thread(target=self._run, name="notifier", daemon=True)
            self._worker.start()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        log.info("notify: %s", text)
        if not self.enabled:
            return
        if self.spool_path:
            try:
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT COUNT(*) FROM notification").fetchone()[0] >= self.max_pending:
                        raise queue.Full
                    row = db.execute("INSERT INTO notification(text) VALUES(?)", (f"[{self.prefix}] {text}"[:4000],))
                    db.execute("INSERT INTO notification_time VALUES(?,?)", (row.lastrowid, time.time()))
                self.enqueue_error = False
            except (sqlite3.Error, OSError, queue.Full):
                self.failed += 1
                self.enqueue_error = True
                log.error("notification could not be queued; new exposure will be blocked until delivery recovers")
            self._wake.set()
            return
        try:
            self._q.put_nowait(f"[{self.prefix}] {text}"[:4000])
        except queue.Full:
            self.failed += 1

    def send_now(self, text: str) -> bool:
        """Synchronous send, for CLI commands. Returns success."""
        if not self.enabled:
            return False
        return self._post(f"[{self.prefix}] {text}"[:4000])

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait for queued AND in-flight requests; False means the deadline expired."""
        end = time.monotonic() + timeout
        if self.spool_path:
            while self.pending():
                if time.monotonic() >= end:
                    return False
                self._wake.set()
                time.sleep(min(0.05, max(0, end - time.monotonic())))
            return True
        with self._q.all_tasks_done:
            while self._q.unfinished_tasks:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._q.all_tasks_done.wait(remaining)
        return True

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.spool_path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def pending(self):
        if not self.spool_path:
            return self._q.unfinished_tasks
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM notification").fetchone()[0]

    def health(self):
        if not self.enabled:
            return {"enabled": False, "healthy": True, "pending": 0, "oldest_seconds": 0}
        try:
            if self.spool_path:
                with self._db() as db:
                    count, oldest = db.execute(
                        "SELECT COUNT(*),MIN(COALESCE(t.created,0)) FROM notification n LEFT JOIN notification_time t ON n.id=t.id"
                    ).fetchone()
            else:
                count, oldest = self.pending(), None
            age = max(0, time.time() - oldest) if oldest is not None else 0
            return {
                "enabled": True,
                "healthy": not self.enqueue_error and count < self.max_pending and age < 300,
                "pending": count,
                "oldest_seconds": age,
            }
        except (sqlite3.Error, OSError):
            return {"enabled": True, "healthy": False, "error": "outbox unavailable"}

    def _run(self):
        if self.spool_path:
            return self._run_durable()
        while True:
            text = self._q.get()
            try:
                self._post(text)
            except Exception:
                self.failed += 1
                log.error("notification worker failed")
            finally:
                self._q.task_done()

    def _run_durable(self):
        while True:
            try:
                with self._db() as db:
                    row = db.execute("SELECT id,text FROM notification ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    self._wake.wait(1)
                    self._wake.clear()
                    continue
                try:
                    success = self._post(row[1])
                except Exception:
                    success = False
                if success:
                    with self._db() as db:
                        db.execute("DELETE FROM notification WHERE id=?", (row[0],))
                        db.execute("DELETE FROM notification_time WHERE id=?", (row[0],))
                    continue
                log.error("notification remains in durable outbox after delivery failure")
            except (sqlite3.Error, OSError):
                # A transient lock/I/O failure must not silently kill the only delivery worker.
                self.failed += 1
                log.error("notification outbox unavailable; delivery worker will retry")
            self._wake.wait(30)
            self._wake.clear()

    def _post(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=15) as r:
                    if json.loads(r.read()).get("ok"):
                        self.sent += 1
                        return True
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                log.warning("telegram send failed (attempt %d): %s", attempt + 1, type(e).__name__)
            time.sleep(2**attempt)
        self.failed += 1
        return False
