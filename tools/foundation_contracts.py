"""F0.5: validate architecture ownership/dependency map, never certify gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

GATES = tuple(f"F{i}" for i in range(9))
MAP = "docs/architecture/f0-contract-map.json"


class ContractError(ValueError):
    """A documented dependency or code owner cannot be verified."""


def validate(root: Path, path: Path | None = None) -> dict:
    root = root.resolve()
    mapping = path if path is not None else root / MAP
    if mapping.is_symlink() or not mapping.is_file() or not mapping.resolve().is_relative_to(root):
        raise ContractError("architecture registry must be a regular repository file")
    if mapping.stat().st_size > 100_000:
        raise ContractError("architecture registry unexpectedly large")
    try:
        record = json.loads(
            mapping.read_bytes(),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
    except (UnicodeError, ValueError) as exc:
        raise ContractError("architecture registry is invalid JSON") from exc
    if (
        type(record) is not dict
        or record.get("schema_version") != 1
        or record.get("stage") != "F0_ARCHITECTURE_MAP_UNQUALIFIED"
        or record.get("runtime_policy") != "PAPER_ONLY"
        or record.get("qualification") != "BLOCKED"
        or record.get("release_authorization") is not False
    ):
        raise ContractError("architecture registry cannot certify an execution/release gate")
    gates = record.get("gates")
    if type(gates) is not list or [g.get("id") for g in gates if type(g) is dict] != list(GATES):
        raise ContractError("F0-F8 gates must be complete, unique and in causal order")
    for index, gate in enumerate(gates):
        if type(gate) is not dict:
            raise ContractError("invalid gate definition")
        if gate.get("depends_on") != ([] if index == 0 else [GATES[index - 1]]):
            raise ContractError("gate dependency violates F0-F8 foundation-first order")
        if gate.get("status") != "BLOCKED":
            raise ContractError("architecture document cannot self-certify gate status")
        if not isinstance(gate.get("name"), str) or not gate["name"].strip():
            raise ContractError("unnamed foundation gate")
        if not isinstance(gate.get("exit_evidence"), str) or not gate["exit_evidence"].strip():
            raise ContractError("gate exit evidence undefined")
        files = [gate.get("primary_owner"), *(gate.get("implementation_refs") or [])]
        if len(files) != len(set(files)) or any(type(f) is not str for f in files):
            raise ContractError("missing or duplicated code ownership reference")
        for ref in files:
            rel = PurePosixPath(ref)
            if (
                not ref
                or rel.is_absolute()
                or ".." in rel.parts
                or not (root / ref).is_file()
                or (root / ref).is_symlink()
                or not (root / ref).resolve().is_relative_to(root)
            ):
                raise ContractError("invalid or absent referenced contract implementation")
    return {
        "foundation_gates": list(GATES),
        "valid_development_dependencies": True,
        "qualification": "BLOCKED",
        "release_authorization": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--map", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(validate(args.root, args.map), sort_keys=True))
    except (ContractError, OSError) as exc:
        print(f"F0 architecture check refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
