from dataclasses import replace
from datetime import UTC, datetime

import pytest

from lab.execution_store import ExecutionStore
from lab.paper import acquire_runtime_lock, execution_identity, runtime_dir
from lab.persistence import atomic_json, read_json
from lab.runtime_ops import gold_upgrade_plan, run, snapshot_locked


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


def test_upgrade_requires_bound_fresh_flat_broker_evidence_and_preserves_book(goog_lab, tmp_path, monkeypatch):
    import os

    cfg = replace(goog_lab, results_dir=tmp_path / "results")
    rt = runtime_dir(cfg)
    atomic_json(
        rt / "identity.json",
        {
            "account": "DU1234567",
            "tickers": ["GOOG"],
            "deployment": {"implementation_sha256": "old"},
            "execution": execution_identity(cfg),
        },
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
    sync = os.fsync
    parent = tmp_path.stat()

    def fail_backup_parent_sync(fd):
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) == (parent.st_dev, parent.st_ino):
            raise OSError("backup parent sync failed")
        return sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr("lab.runtime_ops.os.fsync", fail_backup_parent_sync)
        with pytest.raises(OSError, match="backup parent sync failed"):
            run(cfg, "upgrade", evidence=ev, reason="reviewed", destination=tmp_path / "nested" / "failed-backup")
    assert read_json(rt / "identity.json")["deployment"]["implementation_sha256"] == "old"
    result = run(cfg, "upgrade", evidence=ev, reason="reviewed", destination=tmp_path / "backup")
    assert result["new"] != result["old"]
    assert ExecutionStore(rt / "ledger.json").load()["cash"] == 999
    assert "T" in ExecutionStore(rt / "ledger.json").maps["applied"]


def test_cold_backup_excludes_operator_control_writers(goog_lab, tmp_path, monkeypatch):
    import fcntl
    import shutil

    from lab.authority import Authority

    cfg = replace(goog_lab, results_dir=tmp_path / "results")
    rt = runtime_dir(cfg)
    authority = Authority(rt, {"tickers": ["A"]})
    authority.issue("supervised")
    authority.authorize({"ticker": "A", "qty": 1})
    copy = shutil.copyfile
    copied = []

    def copy_while_operator_tries_to_write(source, target):
        # A second file description uses the same OS lock as renew/approve.
        # It must not acquire a write lease during any part of the snapshot.
        with (rt / "authority.lock").open("a") as operator, pytest.raises(BlockingIOError):
            fcntl.flock(operator, fcntl.LOCK_EX | fcntl.LOCK_NB)
        copied.append(source.name)
        return copy(source, target)

    monkeypatch.setattr("lab.runtime_ops.shutil.copyfile", copy_while_operator_tries_to_write)
    run(cfg, "backup", destination=tmp_path / "backup")
    assert {"authority.key", "authority.json", "approvals.sqlite3"} <= set(copied)
    restored = Authority(tmp_path / "backup", authority.identity)
    assert restored.read() == authority.read()
    assert restored.requests() == authority.requests()


def test_enable_gold_migrates_only_a_broker_attested_flat_runtime(goog_lab, tmp_path):
    cfg = replace(goog_lab, results_dir=tmp_path / "results")
    rt = runtime_dir(cfg)
    identity = {
        "account": "DU1234567",
        "tickers": ["GOOG"],
        "deployment": {"implementation_sha256": "old"},
        "execution": execution_identity(cfg),
    }
    atomic_json(rt / "identity.json", identity)
    ExecutionStore(rt / "ledger.json").save({"cash": 999, "positions": {}, "pending": {}})
    atomic_json(rt / "HALT", {"kind": "review"})
    assert any("hard halt" in blocker for blocker in gold_upgrade_plan(rt, cfg)["blockers"])
    (rt / "HALT").unlink()
    plan = gold_upgrade_plan(rt, cfg)
    evidence = tmp_path / "gold-attestation.json"
    atomic_json(
        evidence,
        {
            "captured_at": datetime.now(UTC).isoformat(),
            "runtime_identity": plan["old"],
            "ledger": plan["ledger"],
            "account": "DU1234567",
            "open_orders": [],
            "positions": {},
        },
    )

    result = run(
        cfg,
        "enable-gold",
        evidence=evidence,
        reason="operator enabled gold after broker reconciliation",
        destination=tmp_path / "gold-backup",
    )

    assert result["action"] == "enable-gold"
    assert read_json(rt / "identity.json")["gold"] == "london-spot-atr-v1"
    assert ExecutionStore(rt / "ledger.json").load()["cash"] == 999
    assert (tmp_path / "gold-backup" / "backup-manifest.json").exists()
