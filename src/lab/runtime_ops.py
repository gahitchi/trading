"""Cold runtime backups and conservative, explicitly evidenced code upgrades."""

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from lab.authority import digest
from lab.execution_store import ExecutionStore
from lab.paper import acquire_runtime_lock, runtime_dir
from lab.persistence import atomic_json, read_json
from lab.provenance import source_digest


def file_hash(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def snapshot_locked(runtime, destination):
    runtime, destination = Path(runtime).resolve(), Path(destination).resolve()
    if destination == runtime or runtime in destination.parents or destination.exists():
        raise ValueError("backup destination must be new and outside the runtime directory")
    destination.mkdir(parents=True, mode=0o700)
    files = {}
    for source in sorted(runtime.rglob("*")):
        if source.is_symlink():
            raise ValueError("runtime backup refuses symlinks")
        if not source.is_file() or source.name in {"LOCK", "identity.lock", "authority.lock"}:
            continue
        relative = source.relative_to(runtime)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target)
        target.chmod(0o600)
        with target.open("rb") as f:
            os.fsync(f.fileno())
        files[str(relative)] = {
            "sha256": file_hash(target),
            "bytes": target.stat().st_size,
        }
    for directory in sorted(
        (p for p in destination.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    atomic_json(
        destination / "backup-manifest.json", {"version": 1, "created": datetime.now(UTC).isoformat(), "files": files}
    )
    return {"destination": str(destination), "files": len(files), "bytes": sum(r["bytes"] for r in files.values())}


def upgrade_plan(runtime):
    runtime = Path(runtime)
    old = read_json(runtime / "identity.json")
    new = {**old, "deployment": {**old["deployment"], "implementation_sha256": source_digest()}}
    ledger_path = runtime / "ledger.json"
    state = (
        ExecutionStore(ledger_path).load()
        if ledger_path.exists() or ledger_path.with_suffix(".sqlite3").exists()
        else None
    )
    blockers = []
    if state is None:
        blockers.append("no persisted execution snapshot to reconcile")
    elif state.get("pending") or any(state.get("positions", {}).values()):
        blockers.append("upgrade requires a flat ledger with no unresolved intents")
    return {"old": digest(old), "new": digest(new), "identity": new, "ledger": digest(state), "blockers": blockers}


def run(cfg, action, *, destination=None, evidence=None, reason=""):
    lock = acquire_runtime_lock(cfg)
    try:
        rt = runtime_dir(cfg)
        if action == "backup":
            if destination is None:
                raise ValueError("backup requires --destination")
            return snapshot_locked(rt, destination)
        plan = upgrade_plan(rt)
        if action == "preflight":
            return {k: v for k, v in plan.items() if k != "identity"}
        if plan["blockers"]:
            raise ValueError("; ".join(plan["blockers"]))
        if not evidence or not reason.strip() or not destination:
            raise ValueError("upgrade requires --evidence, --reason and a new --destination for a cold backup")
        report = read_json(evidence)
        captured = datetime.fromisoformat(report["captured_at"])
        age = (datetime.now(UTC) - captured).total_seconds()
        if (
            not 0 <= age <= 600
            or report.get("runtime_identity") != plan["old"]
            or report.get("ledger") != plan["ledger"]
        ):
            raise ValueError("broker attestation must be fresh and bind the exact old identity and ledger")
        if (
            report.get("account") != plan["identity"]["account"]
            or report.get("open_orders") != []
            or report.get("positions") != {}
        ):
            raise ValueError("broker attestation must report the same paper account, no orders and a flat book")
        result = snapshot_locked(rt, destination)
        receipt = {
            "version": 1,
            "old": plan["old"],
            "new": plan["new"],
            "ledger": plan["ledger"],
            "reason": reason,
            "evidence_sha256": hashlib.sha256(Path(evidence).read_bytes()).hexdigest(),
            "backup": result,
            "at": datetime.now(UTC).isoformat(),
        }
        # Audit first. A crash here leaves old identity (safe to retry). The ledger,
        # its permanent history and all hard halts are never reset by an upgrade.
        atomic_json(rt / f"upgrade-{plan['new']}.json", receipt)
        atomic_json(rt / "identity.json", plan["identity"])
        return receipt
    finally:
        lock.close()
