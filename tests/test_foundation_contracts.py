"""F0.5 must not permit missing dependencies or self-approved foundation gates."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.foundation_contracts import ContractError, validate

ROOT = Path(__file__).resolve().parents[1]


def test_existing_registry_has_all_sequential_foundation_contracts():
    result = validate(ROOT)
    assert result["foundation_gates"] == [f"F{i}" for i in range(9)]
    assert result["qualification"] == "BLOCKED"
    assert result["release_authorization"] is False


@pytest.fixture
def registry(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    record = json.loads((ROOT / "docs/architecture/f0-contract-map.json").read_text())
    for gate in record["gates"]:
        for relative in [gate["primary_owner"], *gate["implementation_refs"]]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("simulated implementation reference\n")
    path = root / "docs/architecture/f0-contract-map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return root, path, record


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["gates"].pop(),
        lambda d: d["gates"].reverse(),
        lambda d: d["gates"][2].update(depends_on=["F0"]),
        lambda d: d["gates"][8].update(status="QUALIFIED"),
        lambda d: d.update(release_authorization=True),
        lambda d: d.update(runtime_policy="LIVE"),
        lambda d: d["gates"][3].update(primary_owner="../secrets/id"),
        lambda d: d["gates"][4].update(implementation_refs=[]),
        lambda d: d["gates"][5].update(implementation_refs=[{"path": "x"}]),
    ],
)
def test_architecture_contract_rejects_invalid_or_self_qualified_records(registry, mutate):
    root, path, record = registry
    altered = copy.deepcopy(record)
    mutate(altered)
    path.write_text(json.dumps(altered))
    with pytest.raises(ContractError):
        validate(root)


def test_architecture_contract_rejects_missing_implementation_reference(registry):
    root, path, record = registry
    target = root / record["gates"][0]["primary_owner"]
    target.unlink()
    with pytest.raises(ContractError, match="invalid or absent"):
        validate(root, path)


def test_architecture_contract_rejects_nonfinite_json(registry):
    root, path, _ = registry
    path.write_text('{"schema_version": NaN}')
    with pytest.raises(ContractError, match="invalid JSON"):
        validate(root)


def test_architecture_contract_rejects_external_symlink(registry, tmp_path):
    root, path, _ = registry
    external = tmp_path / "foreign.json"
    external.write_text(path.read_text())
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(ContractError, match="regular repository file"):
        validate(root, path)
