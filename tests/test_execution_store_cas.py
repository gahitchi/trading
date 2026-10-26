"""Snapshot compare-and-swap and immutable broker execution attribution.

These are offline SQLite regressions. No broker, credentials or network.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from unittest.mock import patch

import pytest

from lab.execution_store import ExecutionStore
from lab.persistence import read_json


def test_first_two_writers_from_empty_db_are_exclusive(tmp_path):
    path = tmp_path / "ledger.json"
    first = ExecutionStore(path)
    stale = ExecutionStore(path)
    first.maps["applied"]["T1"] = [1, "O1"]
    first.save({"cash": 900, "positions": {"A": 1}})
    stale.maps["applied"]["T2"] = [2, "O2"]
    with pytest.raises(ValueError, match="stale execution snapshot writer"):
        stale.save({"cash": 800, "positions": {"A": 2}})
    reread = ExecutionStore(path)
    assert reread.load()["cash"] == 900
    assert reread.maps["applied"]["T1"] == [1, "O1"]
    assert "T2" not in reread.maps["applied"]
    assert read_json(path)["cash"] == 900


def test_existing_head_cannot_be_replaced_by_stale_worker(tmp_path):
    path = tmp_path / "ledger.json"
    ExecutionStore(path).save({"cash": 1000})
    first, stale = ExecutionStore(path), ExecutionStore(path)
    assert first.load()["cash"] == stale.load()["cash"] == 1000
    first.save({"cash": 975, "pending": {"O1": {"ticker": "A", "side": "BUY", "qty": 1}}})
    with pytest.raises(ValueError, match="stale execution snapshot writer"):
        stale.save({"cash": 1100})
    final = ExecutionStore(path).load()
    assert final["cash"] == 975 and "O1" in final["pending"]


def test_aba_saving_identical_state_still_advances_generation(tmp_path):
    path = tmp_path / "ledger.json"
    ExecutionStore(path).save({"cash": 1000})
    writer_a, writer_b = ExecutionStore(path), ExecutionStore(path)
    before = writer_a.load()
    writer_a.save({key: value for key, value in before.items() if key not in ("version", "authoritative_store")})
    assert writer_a.load() == before
    with pytest.raises(ValueError, match="stale execution snapshot writer"):
        writer_b.save({"cash": 900})
    assert ExecutionStore(path).load()["cash"] == 1000
    with sqlite3.connect(path.with_suffix(".sqlite3")) as db:
        assert db.execute("SELECT generation FROM snapshot WHERE id=1").fetchone()[0] == 2


def test_parallel_competing_writers_commit_only_one_snapshot(tmp_path):
    path = tmp_path / "ledger.json"
    ExecutionStore(path).save({"cash": 1000})
    barrier = Barrier(2)

    def update(amount):
        store = ExecutionStore(path)
        barrier.wait(timeout=10)
        try:
            store.save({"cash": amount})
        except ValueError as exc:
            assert "stale execution snapshot writer" in str(exc)
            return "refused"
        return "committed"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(update, (1100, 1200)))
    assert sorted(results) == ["committed", "refused"]
    assert ExecutionStore(path).load()["cash"] in (1100, 1200)


def test_applied_execution_id_cannot_be_rewritten_in_pending_batch(tmp_path):
    store = ExecutionStore(tmp_path / "ledger.json")
    store.maps["applied"]["T1"] = [1, "O1"]
    with pytest.raises(ValueError, match="conflicting applied trade ID"):
        store.maps["applied"]["T1"] = [2, "O2"]
    assert store.maps["applied"]["T1"] == [1, "O1"]
    store.save({"cash": 900})
    assert ExecutionStore(tmp_path / "ledger.json").maps["applied"]["T1"] == [1, "O1"]


def test_committed_trade_identity_is_immutable_but_identical_redelivery_is_idempotent(tmp_path):
    path = tmp_path / "ledger.json"
    original = ExecutionStore(path)
    original.maps["applied"]["T1"] = [15, "O1"]
    original.save({"cash": 900})
    restarted = ExecutionStore(path)
    restarted.maps["applied"]["T1"] = [15, "O1"]
    restarted.save({"cash": 900})
    assert len(ExecutionStore(path).maps["applied"]) == 1
    attacker = ExecutionStore(path)
    attacker.maps["applied"]["T1"] = [16, "O2"]
    with pytest.raises(ValueError, match="conflicting applied trade ID"):
        attacker.save({"cash": 800})
    final = ExecutionStore(path)
    assert final.load()["cash"] == 900 and final.maps["applied"]["T1"] == [15, "O1"]


def test_conflicting_trade_rolls_back_other_history_and_cash(tmp_path):
    path = tmp_path / "ledger.json"
    original = ExecutionStore(path)
    original.maps["applied"]["T1"] = [15, "O1"]
    original.save({"cash": 900})
    writer = ExecutionStore(path)
    writer.maps["terminal"]["C1"] = {"ticker": "A", "side": "BUY", "qty": 1}
    # Bypass the public mapping setter: the SQLite transaction must be the
    # final authority even if a caller tampers with its staged dictionary.
    writer.maps["applied"].dirty["T1"] = [99, "FORGED"]
    with pytest.raises(ValueError, match="conflicting applied trade ID"):
        writer.save({"cash": 800})
    recovered = ExecutionStore(path)
    assert recovered.load()["cash"] == 900
    assert "C1" not in recovered.maps["terminal"]
    assert recovered.maps["applied"]["T1"] == [15, "O1"]


def test_v1_database_upgrades_without_losing_state_or_history(tmp_path):
    path = tmp_path / "ledger.json"
    database = path.with_suffix(".sqlite3")
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE history (bucket TEXT NOT NULL, id TEXT NOT NULL, "
            "value TEXT NOT NULL, coid TEXT, PRIMARY KEY(bucket,id)) WITHOUT ROWID"
        )
        db.execute("CREATE INDEX history_order ON history(bucket,coid)")
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO snapshot(id,value) VALUES(1,?)",
            (json.dumps({"cash": 750, "version": 3, "authoritative_store": database.name}),),
        )
        db.execute(
            "INSERT INTO history(bucket,id,value,coid) VALUES(?,?,?,?)",
            ("applied", "OLD", '[2,"O-OLD"]', "O-OLD"),
        )
    store = ExecutionStore(path)
    assert store.load()["cash"] == 750
    assert store.maps["applied"]["OLD"] == [2, "O-OLD"]
    store.save({"cash": 735})
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT generation FROM snapshot WHERE id=1").fetchone()[0] == 1
    assert ExecutionStore(path).maps["applied"]["OLD"] == [2, "O-OLD"]


def test_interrupted_schema_upgrade_rolls_back_ddl_and_version(tmp_path):
    path = tmp_path / "ledger.json"
    database = path.with_suffix(".sqlite3")
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE history (bucket TEXT NOT NULL, id TEXT NOT NULL, "
            "value TEXT NOT NULL, coid TEXT, PRIMARY KEY(bucket,id)) WITHOUT ROWID"
        )
        db.execute("CREATE INDEX history_order ON history(bucket,coid)")
        db.execute("PRAGMA user_version=1")

    original_connect = ExecutionStore.connect

    @contextmanager
    def interrupting_connect(self):
        with original_connect(self) as db:

            class InterruptedConnection:
                def execute(self, sql, *args):
                    if sql == "PRAGMA user_version=3":
                        raise OSError("simulated migration interruption")
                    return db.execute(sql, *args)

            yield InterruptedConnection()

    with (
        patch.object(ExecutionStore, "connect", interrupting_connect),
        pytest.raises(OSError, match="migration interruption"),
    ):
        ExecutionStore(path)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "generation" not in {column[1] for column in db.execute("PRAGMA table_info(snapshot)")}
    assert ExecutionStore(path)._expected_generation == 0
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_failed_projection_after_commit_does_not_reuse_stale_generation(tmp_path):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.save({"cash": 1000})
    with (
        patch("lab.execution_store.atomic_json", side_effect=OSError("simulated power loss")),
        pytest.raises(OSError, match="power loss"),
    ):
        store.save({"cash": 900})
    assert read_json(path)["cash"] == 1000
    store.save({"cash": 850})
    assert ExecutionStore(path).load()["cash"] == 850


def test_unknown_execution_db_schema_never_downgraded(tmp_path):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.save({"cash": 1000})
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="unknown execution database version"):
        ExecutionStore(path)
