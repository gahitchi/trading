"""Adversarial F0 local-wheel reproducibility and source-parity contract tests."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from tools import foundation_manifest as source
from tools import foundation_wheel as wheel


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root):
    _git(root, "add", ".")
    _git(root, "-c", "user.name=F0", "-c", "user.email=f0@example.invalid", "commit", "-qm", "fixture")


def _encode_record(archive_items, *, omission=None, corrupted=None):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    for name, data in archive_items.items():
        if name == omission:
            continue
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if name == corrupted:
            encoded = "A" * len(encoded)
        writer.writerow([name, "sha256=" + encoded, str(len(data))])
    writer.writerow(["trading_lab-0.2.0.dist-info/RECORD", "", ""])
    return out.getvalue().encode()


def _wheel_file(path: Path, *, body=b"print('ok')\n", extra=None, omitted=None, corrupted=None, version="0.2.0"):
    contents = {
        "lab/__init__.py": body,
        "lab/default.toml": b"[risk]\nprofile = 'balanced'\n",
        "trading_lab-0.2.0.dist-info/METADATA": f"Name: trading-lab\nVersion: {version}\n\n".encode(),
        "trading_lab-0.2.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
    }
    if extra is not None:
        contents[extra] = b"injected extra file"
    record = _encode_record(contents, omission=omitted, corrupted=corrupted)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in contents.items():
            z.writestr(name, data)
        z.writestr("trading_lab-0.2.0.dist-info/RECORD", record)


@pytest.fixture
def pair(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    for relative in source.INPUTS:
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        if relative == source.SCOPE:
            text = json.dumps(
                {
                    "schema_version": 1,
                    "stage": "F0_REFERENCE_SLICE_UNQUALIFIED",
                    "operator": {"authority": "NONE_IN_F0", "account_mode": "PAPER_ONLY"},
                }
            )
        elif relative == "pyproject.toml":
            text = '[project]\nname = "trading-lab"\nversion = "0.2.0"\n'
        else:
            text = "synthetic pinned manifest\n"
        file.write_text(text)
    (root / "src/lab").mkdir(parents=True)
    (root / "src/lab/__init__.py").write_bytes(b"print('ok')\n")
    (root / "src/lab/default.toml").write_bytes(b"[risk]\nprofile = 'balanced'\n")
    _commit(root)
    first, second = tmp_path / "first.whl", tmp_path / "second.whl"
    _wheel_file(first)
    _wheel_file(second)
    return root, first, second, tmp_path / "receipt.json"


def test_local_repeat_build_records_unqualified_evidence(pair):
    root, first, second, receipt = pair
    obj = wheel.observation(root, first, second)
    assert obj["candidate_commit"] == source.inventory(root)["candidate_commit"]
    assert obj["wheel"]["source_members"] == 2
    assert obj["wheel"]["wheel_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert obj["qualification"] == "BLOCKED"
    assert obj["release_authorization"] is False
    wheel.capture(root, first, second, receipt)
    assert wheel.verify(root, first, second, receipt)["verified"]
    with pytest.raises(FileExistsError):
        wheel.capture(root, first, second, receipt)


@pytest.mark.parametrize(
    ("bad_kwargs", "error"),
    [
        ({"body": b"tampered code"}, "differ from committed sources"),
        ({"extra": "lab/hidden.py"}, "differ from committed sources"),
        ({"extra": "other/evil.py"}, "unexpected executable"),
        ({"omitted": "lab/__init__.py"}, "does not cover every member"),
        ({"corrupted": "lab/__init__.py"}, "member digest mismatch"),
        ({"version": "0.3.0"}, "version differs"),
    ],
)
def test_reject_incorrect_source_or_wheel_record(pair, bad_kwargs, error):
    root, first, second, _ = pair
    _wheel_file(second, **bad_kwargs)
    with pytest.raises(source.FoundationError, match=error):
        wheel.observation(root, first, second)


def test_reject_second_build_with_different_archive_bytes(pair):
    root, first, second, _ = pair
    _wheel_file(second)
    with zipfile.ZipFile(second, "a") as z:
        z.comment = b"non-reproducible archive metadata"
    with pytest.raises(source.FoundationError, match="repeat-built"):
        wheel.observation(root, first, second)


def test_reject_single_artifact_masquerading_as_two_builds(pair):
    root, first, _, _ = pair
    with pytest.raises(source.FoundationError, match="two distinct"):
        wheel.observation(root, first, first)


def test_refuse_dirty_or_changed_source_candidate(pair):
    root, first, second, receipt = pair
    wheel.capture(root, first, second, receipt)
    (root / "src/lab/__init__.py").write_text("print('altered')\n")
    with pytest.raises(source.FoundationError, match="uncommitted"):
        wheel.verify(root, first, second, receipt)
    _commit(root)
    with pytest.raises(source.FoundationError, match="differ from committed sources"):
        wheel.verify(root, first, second, receipt)


def test_refuse_receipt_forged_to_claim_qualification(pair):
    root, first, second, receipt = pair
    wheel.capture(root, first, second, receipt)
    record = json.loads(receipt.read_text())
    record["release_authorization"] = True
    receipt.write_text(json.dumps(record))
    with pytest.raises(source.FoundationError, match="no longer matches"):
        wheel.verify(root, first, second, receipt)


def test_refuse_evidence_inside_git_checkout(pair):
    root, first, second, _ = pair
    with pytest.raises(source.FoundationError, match="outside"):
        wheel.capture(root, first, second, root / "claim.json")


def test_refuse_duplicate_wheel_members(pair):
    root, first, second, _ = pair
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(second, "a") as z:
            z.writestr("lab/__init__.py", b"print('ok')\n")
    with pytest.raises(source.FoundationError, match="unsafe or duplicate"):
        wheel.observation(root, first, second)
