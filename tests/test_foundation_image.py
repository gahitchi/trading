"""F0.2 build receipts refuse forged image metadata and runtime dependencies."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools import foundation_image as image_gate
from tools import foundation_manifest as source


def _commit(root: Path):
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=F0", "-c", "user.email=f0@example.invalid", "commit", "-qm", "synthetic"],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def fake_build(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    for name in source.INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == source.SCOPE:
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stage": "F0_REFERENCE_SLICE_UNQUALIFIED",
                        "operator": {"authority": "NONE_IN_F0", "account_mode": "PAPER_ONLY"},
                    }
                )
            )
        else:
            path.write_text("synthetic tracked input\n")
    _commit(root)
    image = tmp_path / "image.json"
    installed = tmp_path / "installed.json"
    image.write_text(
        json.dumps(
            {
                "id": "sha256:" + "a" * 64,
                "os": "linux",
                "architecture": "amd64",
                "user": "lab",
                "rootfs_layers": ["sha256:" + "b" * 64, "sha256:" + "c" * 64],
            }
        )
    )
    installed.write_text(
        json.dumps(
            {
                "os_inventory_sha256": "d" * 64,
                "installed_python_sha256": "e" * 64,
                "installed_python_count": 30,
                "nautilus_version": "2.0.0rc7",
                "lab_version": "0.2.0",
            }
        )
    )
    return root, image, installed, tmp_path / "receipt.json"


def test_exact_runtime_observation_is_unqualified_and_write_once(fake_build):
    root, image, installed, receipt = fake_build
    image_gate.capture(root, image, installed, receipt)
    obj = json.loads(receipt.read_text())
    assert obj["image"]["id"] == "sha256:" + "a" * 64
    assert obj["qualification"] == "BLOCKED"
    assert obj["release_authorization"] is False
    assert obj["broker_authorization"] is False
    assert image_gate.verify(root, image, installed, receipt)["build_observation_matches"]
    with pytest.raises(FileExistsError):
        image_gate.capture(root, image, installed, receipt)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", "sha256:invalid"),
        ("os", "windows"),
        ("architecture", "not-a-platform"),
        ("user", "root"),
        ("rootfs_layers", []),
        ("rootfs_layers", ["sha256:" + "0" * 64, "invalid"]),
    ],
)
def test_f0_rejects_invalid_or_privileged_container_identity(fake_build, key, value):
    root, image, installed, receipt = fake_build
    obj = json.loads(image.read_text())
    obj[key] = value
    image.write_text(json.dumps(obj))
    with pytest.raises(source.FoundationError):
        image_gate.capture(root, image, installed, receipt)
    assert not receipt.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("installed_python_sha256", "bad"),
        ("installed_python_count", 0),
        ("installed_python_count", True),
        ("nautilus_version", "0.0.1"),
        ("lab_version", "0.3.0"),
    ],
)
def test_runtime_dependency_observation_must_be_pinned(fake_build, key, value):
    root, image, installed, receipt = fake_build
    obj = json.loads(installed.read_text())
    obj[key] = value
    installed.write_text(json.dumps(obj))
    with pytest.raises(source.FoundationError):
        image_gate.capture(root, image, installed, receipt)


def test_image_or_installed_dependency_changes_invalidate_evidence(fake_build):
    root, image, installed, receipt = fake_build
    image_gate.capture(root, image, installed, receipt)
    obj = json.loads(installed.read_text())
    obj["os_inventory_sha256"] = "1" * 64
    installed.write_text(json.dumps(obj))
    with pytest.raises(source.FoundationError, match="does not match"):
        image_gate.verify(root, image, installed, receipt)
    installed_obj = json.loads(installed.read_text())
    installed_obj["os_inventory_sha256"] = "d" * 64
    installed.write_text(json.dumps(installed_obj))
    meta = json.loads(image.read_text())
    meta["id"] = "sha256:" + "f" * 64
    image.write_text(json.dumps(meta))
    with pytest.raises(source.FoundationError, match="does not match"):
        image_gate.verify(root, image, installed, receipt)


def test_source_commit_and_scope_change_break_image_evidence(fake_build):
    root, image, installed, receipt = fake_build
    image_gate.capture(root, image, installed, receipt)
    (root / "uv.lock").write_text("new pinned version\n")
    with pytest.raises(source.FoundationError, match="uncommitted"):
        image_gate.verify(root, image, installed, receipt)
    _commit(root)
    with pytest.raises(source.FoundationError, match="does not match"):
        image_gate.verify(root, image, installed, receipt)


def test_refuse_nonfinite_or_unauthorized_build_receipt(fake_build):
    root, image, installed, receipt = fake_build
    image_gate.capture(root, image, installed, receipt)
    receipt.write_text('{"qualification": NaN}')
    with pytest.raises(source.FoundationError):
        image_gate.verify(root, image, installed, receipt)
    receipt.write_text(json.dumps({**image_gate.observation(root, image, installed), "release_authorization": True}))
    with pytest.raises(source.FoundationError, match="does not match"):
        image_gate.verify(root, image, installed, receipt)


def test_refuse_in_tree_receipt(fake_build):
    root, image, installed, _ = fake_build
    with pytest.raises(source.FoundationError, match="outside"):
        image_gate.capture(root, image, installed, root / "out.json")
    assert not (root / "out.json").exists()
