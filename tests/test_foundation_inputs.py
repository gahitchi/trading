"""F0 input byte custody is read-only and does not authenticate a market feed."""

from __future__ import annotations

import json
import subprocess

import pytest

from tools import foundation_inputs as inputs
from tools import foundation_manifest as source


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def local_checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q")
    for relative in source.INPUTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == source.SCOPE:
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
            path.write_text("synthetic pinned bytes\n")
    (root / ".gitignore").write_text("data/\n")
    (root / "lab.toml").write_text('[universe]\nSPY = "ARCX"\n\n[trading]\nspeed = "daily"\n')
    git(root, "add", ".")
    git(root, "-c", "user.name=F0", "-c", "user.email=f0@example.invalid", "commit", "-qm", "fixture")
    data = root / "data"
    data.mkdir()
    (data / "manifest.json").write_text(json.dumps({"SPY": {"source": "unverified synthetic fixture"}}))
    (data / "SPY.parquet").write_bytes(b"PAR1" + b"fixture payload" + b"PAR1")
    return root, tmp_path / "f0-local.json"


def test_capture_local_inputs_only_records_redacted_hashes(local_checkout):
    root, receipt = local_checkout
    inputs.capture(root, receipt)
    obj = json.loads(receipt.read_text())
    assert set(obj["local_inputs"]) == set(inputs.ROLES)
    assert obj["qualification"] == "BLOCKED"
    assert obj["release_authorization"] is False
    assert "unverified synthetic fixture" not in receipt.read_text()
    assert "SPY" not in receipt.read_text()
    assert inputs.verify(root, receipt)["byte_identity_matches"] is True
    with pytest.raises(FileExistsError):
        inputs.capture(root, receipt)


@pytest.mark.parametrize(
    ("path", "rewrite"),
    [
        ("lab.toml", '[universe]\nSPY = "XNYS"\n[trading]\nspeed = "daily"\n'),
        ("lab.toml", '[universe]\nSPY = "ARCX"\n[trading]\nspeed = "15m"\n'),
        ("lab.toml", 'universe = "ARCX"\n[trading]\nspeed = "daily"\n'),
        ("lab.toml", '[universe]\nSPY = "ARCX"\nQQQ = "XNAS"\n[trading]\nspeed = "daily"\n'),
        ("lab.toml", '[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n[paper]\naccount_id = "U123"\n'),
        ("lab.toml", '[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n[paper]\nport = 4001\n'),
        ("lab.toml", '[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n[paper]\nport = 7496\n'),
        ("lab.toml", '[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n[paper]\nmode = "live"\n'),
        ("lab.toml", '[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n[paper]\nport = "4002"\n'),
        ("data/manifest.json", '{"QQQ": {"source": "synthetic"}}'),
        ("data/manifest.json", '{"SPY": null}'),
        ("data/SPY.parquet", "bad parquet"),
    ],
)
def test_refuse_invalid_scope_or_missing_reference_bytes(local_checkout, path, rewrite):
    root, _ = local_checkout
    (root / path).write_text(rewrite)
    if path == "lab.toml":
        # Commit the bad configuration: a dirty-tree refusal is not scope validation.
        git(root, "add", path)
        git(root, "-c", "user.name=F0", "-c", "user.email=f0@example.invalid", "commit", "-qm", "bad-scope")
    with pytest.raises(source.FoundationError):
        inputs.observation(root)


def test_data_mutation_revokes_exact_receipt(local_checkout):
    root, receipt = local_checkout
    inputs.capture(root, receipt)
    with (root / "data/SPY.parquet").open("ab") as f:
        f.write(b"changed")
    with pytest.raises(source.FoundationError):
        inputs.verify(root, receipt)


def test_symlink_data_does_not_escape_root(local_checkout, tmp_path):
    root, receipt = local_checkout
    data = root / "data/SPY.parquet"
    data.unlink()
    outside = tmp_path / "foreign.parquet"
    outside.write_bytes(b"PAR1helloPAR1")
    data.symlink_to(outside)
    with pytest.raises(source.FoundationError):
        inputs.capture(root, receipt)


def test_refuse_input_record_inside_checkout(local_checkout):
    root, _ = local_checkout
    with pytest.raises(source.FoundationError, match="outside"):
        inputs.capture(root, root / "data/fake.json")


def test_receipt_cannot_self_approve_market_feed(local_checkout):
    root, receipt = local_checkout
    inputs.capture(root, receipt)
    data = json.loads(receipt.read_text())
    data["qualification"] = "QUALIFIED"
    receipt.write_text(json.dumps(data))
    with pytest.raises(source.FoundationError, match="differs"):
        inputs.verify(root, receipt)


def test_paper_account_reference_config_is_redacted(local_checkout):
    root, receipt = local_checkout
    (root / "lab.toml").write_text(
        '[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n[paper]\naccount_id = "DUTEST123"\nport = 4002\n'
    )
    git(root, "add", "lab.toml")
    git(root, "-c", "user.name=F0", "-c", "user.email=f0@example.invalid", "commit", "-qm", "paper-config")
    inputs.capture(root, receipt)
    assert "DUTEST123" not in receipt.read_text()
    assert inputs.verify(root, receipt)["qualification"] == "BLOCKED"


def test_explicit_private_pilot_config_does_not_mutate_tracked_operator_config(local_checkout, tmp_path):
    root, receipt = local_checkout
    private = tmp_path / "f0-reference.toml"
    private.write_text('[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n[paper]\nport = 4002\n')
    # The default repository configuration is intentionally broader. Only the
    # explicitly supplied private scope file may be used for this observation.
    (root / "lab.toml").write_text('[universe]\nSPY = "ARCX"\nQQQ = "XNAS"\n[trading]\nspeed = "daily"\n')
    git(root, "add", "lab.toml")
    git(root, "-c", "user.name=F0", "-c", "user.email=f0@example.invalid", "commit", "-qm", "normal-config")
    with pytest.raises(source.FoundationError, match="isolated"):
        inputs.observation(root)
    inputs.capture(root, receipt, config=private)
    record = json.loads(receipt.read_text())
    assert str(private) not in receipt.read_text()
    assert "SPY" not in receipt.read_text()
    assert record["qualification"] == "BLOCKED"
    assert inputs.verify(root, receipt, config=private)["byte_identity_matches"] is True
    with pytest.raises(source.FoundationError, match="differs"):
        inputs.verify(root, receipt)
    private.write_text('[universe]\nSPY = "ARCX"\n[trading]\nspeed = "15m"\n')
    with pytest.raises(source.FoundationError):
        inputs.verify(root, receipt, config=private)


def test_explicit_pilot_symlink_is_refused(local_checkout, tmp_path):
    root, _ = local_checkout
    original = tmp_path / "pilot.toml"
    original.write_text('[universe]\nSPY = "ARCX"\n[trading]\nspeed = "daily"\n')
    alias = tmp_path / "alias.toml"
    alias.symlink_to(original)
    with pytest.raises(source.FoundationError, match="unsafe"):
        inputs.observation(root, config=alias)
