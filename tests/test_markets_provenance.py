"""Preserve legacy equity implementation identity while binding crypto code."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lab.provenance import NOT_SIMULATION, runtime_digest, simulation_digest


def test_equity_digest_matches_legacy_file_scope() -> None:
    root = Path(__file__).parents[1] / "src" / "lab"
    expected = hashlib.sha256()
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        if rel in NOT_SIMULATION or rel.startswith("markets/"):
            continue
        expected.update(rel.encode())
        expected.update(file.read_bytes())
    assert simulation_digest() == expected.hexdigest()
    assert simulation_digest(market="equity") == expected.hexdigest()
    assert simulation_digest(market="crypto") != simulation_digest(market="equity")
    assert runtime_digest() != simulation_digest(market="equity")


def test_unrecognized_market_does_not_silently_change_approval_scope() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        simulation_digest(market="unknown")
