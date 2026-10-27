"""F0 provenance is necessary source evidence, never release or broker qualification."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.foundation_manifest import INPUTS, SCOPE, FoundationError, create, inventory, verify

ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    for path in INPUTS:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stage": "F0_REFERENCE_SLICE_UNQUALIFIED",
                    "operator": {"authority": "NONE_IN_F0", "account_mode": "PAPER_ONLY"},
                }
            )
            if path == SCOPE
            else "bounded synthetic pinned input\n"
        )
    _git(root, "add", ".")
    _git(root, "-c", "user.email=test@example.invalid", "-c", "user.name=F0", "commit", "-qm", "fixture")
    return root


def test_f0_write_once_source_inventory_is_unqualified_and_verifiable(checkout: Path, tmp_path: Path):
    receipt = tmp_path / "f0-evidence.json"
    create(checkout, receipt)
    parsed = json.loads(receipt.read_text())
    assert parsed["candidate_commit"] == inventory(checkout)["candidate_commit"]
    assert parsed["qualification"] == "BLOCKED"
    assert parsed["broker_authorization"] is False
    assert parsed["release_authorization"] is False
    assert len(parsed["pinned_inputs_sha256"]) == len(INPUTS)
    assert "requirements.lock" in parsed["pinned_inputs_sha256"]
    assert "lab.toml" not in receipt.read_text()
    assert verify(checkout, receipt)["qualification"] == "BLOCKED"
    with pytest.raises(FileExistsError):
        create(checkout, receipt)
    if os.name == "posix":
        assert receipt.stat().st_mode & 0o077 == 0


def test_f0_refuses_dirty_checkout_and_does_not_write_evidence(checkout: Path, tmp_path: Path):
    (checkout / "uv.lock").write_text("uncommitted lock change")
    output = tmp_path / "candidate.json"
    with pytest.raises(FoundationError, match="uncommitted"):
        create(checkout, output)
    assert not output.exists()


def test_f0_refuses_untracked_source_and_inside_repo_output(checkout: Path, tmp_path: Path):
    (checkout / "unexpected.py").write_text("print('new source')\n")
    with pytest.raises(FoundationError, match="uncommitted"):
        create(checkout, tmp_path / "out.json")
    (checkout / "unexpected.py").unlink()
    with pytest.raises(FoundationError, match="outside"):
        create(checkout, checkout / "out.json")
    assert not (checkout / "out.json").exists()


def test_f0_verification_fails_for_changed_candidate_commit(checkout: Path, tmp_path: Path):
    receipt = tmp_path / "candidate.json"
    create(checkout, receipt)
    (checkout / "uv.lock").write_text("new locked version")
    _git(checkout, "add", "uv.lock")
    _git(checkout, "-c", "user.email=test@example.invalid", "-c", "user.name=F0", "commit", "-qm", "change")
    with pytest.raises(FoundationError, match="differs"):
        verify(checkout, receipt)


def test_f0_verification_fails_for_mutated_receipt_and_environment(checkout: Path, tmp_path: Path, monkeypatch):
    receipt = tmp_path / "candidate.json"
    create(checkout, receipt)
    parsed = json.loads(receipt.read_text())
    parsed["broker_authorization"] = True
    receipt.write_text(json.dumps(parsed))
    with pytest.raises(FoundationError, match="differs"):
        verify(checkout, receipt)
    receipt.unlink()
    create(checkout, receipt)
    monkeypatch.setattr("tools.foundation_manifest.platform.release", lambda: "different-host-kernel")
    with pytest.raises(FoundationError, match="differs"):
        verify(checkout, receipt)


@pytest.mark.parametrize(
    ("authority", "account_mode"),
    [("BROKER_ORDER", "PAPER_ONLY"), ("NONE_IN_F0", "LIVE"), ("NONE_IN_F0", "UNKNOWN")],
)
def test_f0_scope_cannot_claim_broker_authority(checkout: Path, authority: str, account_mode: str):
    scope = checkout / SCOPE
    obj = json.loads(scope.read_text())
    obj["operator"] = {"authority": authority, "account_mode": account_mode}
    scope.write_text(json.dumps(obj))
    _git(checkout, "add", SCOPE)
    _git(checkout, "-c", "user.email=test@example.invalid", "-c", "user.name=F0", "commit", "-qm", "bad-scope")
    with pytest.raises(FoundationError, match="scope cannot assert"):
        inventory(checkout)


def test_f0_rejects_in_tree_symlink_and_nonfinite_receipt(checkout: Path, tmp_path: Path):
    out = tmp_path / "candidate.json"
    create(checkout, out)
    out.write_text('{"schema_version": NaN}')
    with pytest.raises(FoundationError, match="invalid recorded"):
        verify(checkout, out)
    out.unlink()
    (tmp_path / "actual.json").write_text("{}")
    out.symlink_to(tmp_path / "actual.json")
    with pytest.raises(FoundationError, match="symlinked"):
        verify(checkout, out)


def test_f0_cli_respects_refusal_without_leaking_config(checkout: Path, tmp_path: Path):
    evidence = tmp_path / "receipt.json"
    cmd = [sys.executable, str(ROOT / "tools" / "foundation_manifest.py"), "--root", str(checkout)]
    created = subprocess.run([*cmd, "create", "--out", str(evidence)], capture_output=True, text=True, check=False)
    assert created.returncode == 0, created.stderr
    result = subprocess.run([*cmd, "verify", "--input", str(evidence)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["release_authorization"] is False
    (checkout / "untracked-private.txt").write_text("account-secret-value")
    rejected = subprocess.run([*cmd, "verify", "--input", str(evidence)], capture_output=True, text=True, check=False)
    assert rejected.returncode == 2
    assert "account-secret-value" not in rejected.stderr
    assert not rejected.stdout
