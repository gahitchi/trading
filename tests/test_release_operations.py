"""Failure injection at disk and concurrent notification boundaries."""

import errno
import sqlite3
import threading

import pytest

from lab.notify import Notifier
from lab.persistence import atomic_json, read_json


@pytest.mark.parametrize("text", ['{"cash":1e999}', '{"cash":1,"cash":2}', '{"cash":NaN}'])
def test_ambiguous_or_nonfinite_json_is_rejected(tmp_path, text):
    path = tmp_path / "ledger.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        read_json(path)


@pytest.mark.parametrize("boundary", ["fsync", "replace"])
def test_failed_atomic_write_preserves_previous_ledger(tmp_path, monkeypatch, boundary):
    import lab.persistence as persistence

    path = tmp_path / "ledger.json"
    atomic_json(path, {"cash": 100})

    def failure(*args):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(persistence.os, boundary, failure)
    with pytest.raises(OSError):
        atomic_json(path, {"cash": 50})
    assert read_json(path) == {"cash": 100}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ledger.json"]


class StopWorker(Exception):
    pass


def _notifier(tmp_path, monkeypatch):
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    return Notifier(token="test", chat_id="test", spool_path=str(tmp_path / "outbox.sqlite3"))


def test_old_sender_acknowledgement_cannot_delete_new_message(tmp_path, monkeypatch):
    n = _notifier(tmp_path, monkeypatch)
    n.send("old alert")
    sent = []

    def first_delivery(text):
        sent.append(text)
        # A second worker acknowledged the same old row while this worker was sending.
        with n._db() as db:
            db.execute("DELETE FROM notification")
        n.send("new alert")
        return True

    monkeypatch.setattr(n, "_post", first_delivery)
    real_db = n._db
    calls = 0

    def db_until_ack():
        nonlocal calls
        calls += 1
        if calls > 4:  # select, simulated other-worker delete, send, this worker acknowledgement
            raise StopWorker
        return real_db()

    monkeypatch.setattr(n, "_db", db_until_ack)
    with pytest.raises(StopWorker):
        n._run_durable()
    monkeypatch.setattr(n, "_db", real_db)
    with n._db() as db:
        assert db.execute("SELECT text FROM notification").fetchall() == [("[trading-lab] new alert",)]
    assert sent == ["[trading-lab] old alert"]


def test_existing_outbox_is_migrated_without_dropping_alerts(tmp_path, monkeypatch):
    with sqlite3.connect(tmp_path / "outbox.sqlite3") as db:
        db.execute("CREATE TABLE notification (id INTEGER PRIMARY KEY, text TEXT)")
        db.execute("INSERT INTO notification VALUES(42, 'saved alert')")
    n = _notifier(tmp_path, monkeypatch)
    with n._db() as db:
        assert db.execute("SELECT * FROM notification").fetchall() == [(42, "saved alert")]
        db.execute("DELETE FROM notification")
    n.send("next")
    with n._db() as db:
        assert db.execute("SELECT id FROM notification").fetchone()[0] > 42


def test_transient_outbox_failure_does_not_kill_worker(tmp_path, monkeypatch):
    n = _notifier(tmp_path, monkeypatch)
    n.send("saved alert")
    real_db = n._db
    attempts, delivered = [], []

    def flaky_db():
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("temporarily locked")
        return real_db()

    def wait(seconds):
        if seconds == 1:
            raise StopWorker

    monkeypatch.setattr(n, "_db", flaky_db)
    monkeypatch.setattr(n._wake, "wait", wait)
    monkeypatch.setattr(n, "_post", lambda text: delivered.append(text) or True)
    with pytest.raises(StopWorker):
        n._run_durable()
    assert delivered == ["[trading-lab] saved alert"] and n.pending() == 0 and n.failed == 1
