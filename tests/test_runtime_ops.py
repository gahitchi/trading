from dataclasses import replace
from datetime import UTC, datetime

import pytest

from lab.execution_store import ExecutionStore
from lab.paper import acquire_runtime_lock, runtime_dir
from lab.persistence import atomic_json, read_json
from lab.runtime_ops import run, snapshot_locked


def test_backup_is_cold_private_and_refuses_live_writer(goog_lab, tmp_path):
    cfg = replace(goog_lab, results_dir=tmp_path / "results")
    rt = runtime_dir(cfg)
    rt.mkdir(parents=True)
    (rt / "authority.key").write_bytes(b"x" * 32)
    ExecutionStore(rt / "ledger.json").save({"cash": 1000})
    lock = acquire_runtime_lock(cfg)
    with pytest.raises(Exception, match="another paper node"):
        run(cfg, "backup", destination=tmp_path / "backup")
    lock.close()
    report = run(cfg, "backup", destination=tmp_path / "backup")
    assert report["files"] == 3
    assert (tmp_path / "backup" / "authority.key").stat().st_mode & 0o777 == 0o600
    assert read_json(tmp_path / "backup" / "backup-manifest.json")["files"]["ledger.sqlite3"]["bytes"] > 0
    with pytest.raises(ValueError, match="new and outside"):
        snapshot_locked(rt, rt / "backup")


def test_upgrade_requires_bound_fresh_flat_broker_evidence_and_preserves_book(goog_lab, tmp_path):
    cfg = replace(goog_lab, results_dir=tmp_path / "results")
    rt = runtime_dir(cfg)
    atomic_json(
        rt / "identity.json",
        {"account": "DU1234567", "tickers": ["GOOG"], "deployment": {"implementation_sha256": "old"}},
    )
    store = ExecutionStore(rt / "ledger.json")
    store.maps["applied"]["T"] = [1, "X"]
    store.save({"cash": 999, "positions": {}, "pending": {}})
    plan = run(cfg, "preflight")
    with pytest.raises(ValueError, match="requires --evidence"):
        run(cfg, "upgrade")
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "runtime_identity": plan["old"],
        "ledger": plan["ledger"],
        "account": "DU1234567",
        "open_orders": [],
        "positions": {},
    }
    ev = tmp_path / "broker.json"
    atomic_json(ev, {**report, "positions": {"GOOG": 1}})
    with pytest.raises(ValueError, match="flat book"):
        run(cfg, "upgrade", evidence=ev, reason="reviewed", destination=tmp_path / "backup")
    atomic_json(ev, report)
    result = run(cfg, "upgrade", evidence=ev, reason="reviewed", destination=tmp_path / "backup")
    assert result["new"] != result["old"]
    assert ExecutionStore(rt / "ledger.json").load()["cash"] == 999
    assert "T" in ExecutionStore(rt / "ledger.json").maps["applied"]
