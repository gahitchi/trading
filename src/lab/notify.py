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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("lab.notify")


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, prefix: str = "trading-lab"):
        self.token = token if token is not None else os.environ.get("LAB_TELEGRAM_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("LAB_TELEGRAM_CHAT_ID", "")
        self.prefix = prefix
        self.sent = 0
        self.failed = 0
        self._q: queue.Queue = queue.Queue(maxsize=500)
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
        with self._q.all_tasks_done:
            while self._q.unfinished_tasks:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._q.all_tasks_done.wait(remaining)
        return True

    def _run(self):
        while True:
            text = self._q.get()
            try:
                self._post(text)
            except Exception:
                self.failed += 1
                log.error("notification worker failed")
            finally:
                self._q.task_done()

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
                log.warning("telegram send failed (attempt %d): %s", attempt + 1, e)
            time.sleep(2**attempt)
        self.failed += 1
        return False
