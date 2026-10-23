import sqlite3
from unittest.mock import patch

import pytest

from lab.execution_store import ExecutionStore
from lab.persistence import atomic_json, read_json


def test_migration_and_indexed_restart_keep_all_attribution(tmp_path):
    p = tmp_path / "ledger.json"
    intent = {"ticker": "A", "side": "BUY", "qty": 10, "filled": 4}
    atomic_json(
        p, {"cash": 960, "positions": {"A": 4}, "terminal": {"X": intent}, "applied_trades": ["legacy", ["T", 1, "X"]]}
    )
    store = ExecutionStore(p)
    assert store.load()["cash"] == 960
    assert store.maps["terminal"]["X"] == intent
    assert store.maps["applied"].order_has_fill("X")
    assert "legacy" in store.maps["applied"]
    assert all(not m.dirty for m in store.maps.values())
    assert "applied_trades" not in read_json(p)
    assert len(ExecutionStore(p).maps["applied"]) == 2


def test_crash_after_db_commit_before_projection_recovers_atomically(tmp_path):
    p = tmp_path / "ledger.json"
    store = ExecutionStore(p)
    store.save({"cash": 1000})
    store.maps["applied"]["T"] = [1, "X"]
    with patch("lab.execution_store.atomic_json", side_effect=OSError("power loss")), pytest.raises(OSError):
        store.save({"cash": 900, "positions": {"A": 10}})
    assert read_json(p)["cash"] == 1000  # diagnostic projection may lag
    restarted = ExecutionStore(p)
    assert restarted.load()["cash"] == 900 and "T" in restarted.maps["applied"]


def test_transaction_failure_rolls_back_cash_and_trade_together(tmp_path):
    p = tmp_path / "ledger.json"
    store = ExecutionStore(p, max_bytes=64 * 1024)
    store.save({"cash": 1000})
    for i in range(2000):
        store.maps["applied"][f"T-{i}"] = [1, "X" * 100]
    with pytest.raises(sqlite3.OperationalError):
        store.save({"cash": 900})
    restarted = ExecutionStore(p)
    assert restarted.load()["cash"] == 1000 and not restarted.maps["applied"]


def test_missing_database_never_reuses_diagnostic_snapshot(tmp_path):
    p = tmp_path / "ledger.json"
    store = ExecutionStore(p)
    store.save({"cash": 900})
    store.path.unlink()
    with pytest.raises(ValueError, match="database missing"):
        ExecutionStore(p)


def test_large_history_does_not_load_into_memory_or_grow_projection(tmp_path):
    p = tmp_path / "ledger.json"
    store = ExecutionStore(p)
    for batch in range(10):
        for i in range(1000):
            store.maps["applied"][f"T-{batch}-{i}"] = [1, "X"]
        store.save({"cash": 1000})
    restarted = ExecutionStore(p)
    assert len(restarted.maps["applied"]) == 10000
    assert all(not m.dirty for m in restarted.maps.values())
    assert p.stat().st_size < 200 and restarted.maps["applied"]["T-0-0"] == [1, "X"]


def test_interrupted_import_resumes_from_legacy_snapshot(tmp_path):
    p = tmp_path / "ledger.json"
    atomic_json(p, {"cash": 789, "applied_trades": ["T"]})
    with patch.object(ExecutionStore, "save", side_effect=OSError("crash")), pytest.raises(OSError):
        ExecutionStore(p)
    s = ExecutionStore(p)
    assert s.load()["cash"] == 789 and "T" in s.maps["applied"]


def test_projection_prevents_unsafe_downgrade_to_json_only_code(tmp_path):
    p = tmp_path / "ledger.json"
    store = ExecutionStore(p)
    store.save({"cash": 1000})
    assert read_json(p)["version"] == 3
    assert read_json(p)["authoritative_store"] == "ledger.sqlite3"
    assert store.load()["version"] == 3
