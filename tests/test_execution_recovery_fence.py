"""Cold-start and SQLite recovery integrity regressions.

No broker, network, or external order permissions; test intentionally
tampered DB bytes through SQLite to simulate logical file corruption.
"""

from __future__ import annotations

import sqlite3

import pytest

from lab.execution_store import ExecutionStore
from lab.persistence import atomic_json


def test_snapshot_erasure_without_projection_never_resets_account(tmp_path):
    path = tmp_path / "ledger.json"
    original = ExecutionStore(path)
    original.maps["applied"]["T1"] = [1, "O1"]
    original.save({"cash": 900, "positions": {"A": 1}})
    path.unlink()
    with sqlite3.connect(original.path) as db:
        db.execute("DELETE FROM snapshot WHERE id=1")
    with pytest.raises(ValueError, match="existing execution database has no snapshot"):
        ExecutionStore(path)
    with sqlite3.connect(original.path) as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE bucket='applied'").fetchone()[0] == 1


def test_uninitialized_old_database_is_not_mistaken_for_fresh_run(tmp_path):
    path = tmp_path / "ledger.json"
    created = ExecutionStore(path)
    assert created.load() is None
    with pytest.raises(ValueError, match="existing execution database has no snapshot"):
        ExecutionStore(path)



def test_orphaned_history_cannot_be_reimported_from_unbound_old_json(tmp_path):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.maps["applied"]["T1"] = [10, "O1"]
    store.save({"cash": 900, "positions": {"A": 1}})
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM snapshot WHERE id=1")
    # Forging an old, unbound projection must not hide surviving trade IDs
    # while fabricating a fresh monetary opening balance.
    atomic_json(path, {"cash": 1000})
    with pytest.raises(ValueError, match="snapshot missing with recorded history"):
        ExecutionStore(path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE bucket='applied'").fetchone()[0] == 1


def test_missing_snapshot_with_authoritative_projection_is_blocked(tmp_path):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.save({"cash": 1000})
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM snapshot")
    with pytest.raises(ValueError, match="execution snapshot missing"):
        ExecutionStore(path)


def test_legacy_json_import_without_sqlite_snapshot_still_recovers(tmp_path):
    path = tmp_path / "ledger.json"
    atomic_json(path, {"cash": 875, "applied_trades": [["T1", 1, "O1"]]})
    created = ExecutionStore(path)
    assert created.load()["cash"] == 875
    assert created.maps["applied"]["T1"] == [1, "O1"]


@pytest.mark.parametrize(
    "raw",
    [
        '{"cash":1,"cash":1000,"version":3,"authoritative_store":"ledger.sqlite3"}',
        '{"cash":NaN,"version":3,"authoritative_store":"ledger.sqlite3"}',
        '{"cash":1e10000,"version":3,"authoritative_store":"ledger.sqlite3"}',
        '{"cash":1,"version":3,"authoritative_store":"ledger.sqlite3",'
        '"positions":{"A":4,"A":0}}',
    ],
)
def test_sqlite_financial_snapshot_rejects_duplicate_or_nonfinite_fields(tmp_path, raw):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.save({"cash": 1000})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE snapshot SET value=? WHERE id=1", (raw,))
    with pytest.raises(ValueError, match=r"duplicate|nonfinite"):
        ExecutionStore(path)
    with pytest.raises(ValueError, match=r"duplicate|nonfinite"):
        store.load()


def test_applied_history_index_mismatch_is_visible_and_export_refuses(tmp_path):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.maps["applied"]["T1"] = [1, "O1"]
    store.save({"cash": 900})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE history SET coid='OTHER' WHERE bucket='applied' AND id='T1'")
    restarted = ExecutionStore(path)
    with pytest.raises(ValueError, match="order index"):
        _ = restarted.maps["applied"]["T1"]
    with pytest.raises(ValueError, match="order index"):
        restarted.export()


@pytest.mark.parametrize(
    "raw",
    [
        '[1, "O1", "O2"]',
        '[1, "O1", NaN]',
        '[1, "O1", {"ticker":"A","ticker":"B"}]',
        '{"0":1,"1":"O1"}',
    ],
)
def test_applied_history_rejects_invalid_or_duplicate_json(tmp_path, raw):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.maps["applied"]["T1"] = [1, "O1"]
    store.save({"cash": 900})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE history SET value=? WHERE bucket='applied' AND id='T1'", (raw,))
    restarted = ExecutionStore(path)
    with pytest.raises((ValueError, TypeError)):
        _ = restarted.maps["applied"]["T1"]


def test_existing_snapshot_without_projection_is_authoritative(tmp_path):
    path = tmp_path / "ledger.json"
    store = ExecutionStore(path)
    store.maps["applied"]["T1"] = [1, "O1"]
    store.save({"cash": 900})
    path.unlink()
    restarted = ExecutionStore(path)
    assert restarted.load()["cash"] == 900
    assert restarted.maps["applied"]["T1"] == [1, "O1"]
