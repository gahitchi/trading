"""Offline capital-markets CLI contract, including failure and output behavior."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab import cli
from lab.multiasset import MultiAssetError

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "multiasset-offline" / "portfolio.json"


def options(**changes):
    state = {"manifest": str(SAMPLE), "json": True, "output": None}
    state.update(changes)
    return SimpleNamespace(**state)


def test_cli_has_read_only_capital_audit():
    args = cli.build_parser().parse_args(["capital-audit", "--manifest", str(SAMPLE), "--json"])
    assert args.fn is cli.cmd_capital_audit
    assert args.manifest == str(SAMPLE)


def test_cli_reports_reproducible_attributed_stress(capsys):
    cli.cmd_capital_audit(None, options())
    first = json.loads(capsys.readouterr().out)
    cli.cmd_capital_audit(None, options())
    second = json.loads(capsys.readouterr().out)
    assert first == second
    assert first["qualification"] == "RESEARCH_ONLY_NOT_FOR_TRADING"
    assert first["stress_before"]["worst_case_name"] == "severe_combined"
    assert len(first["analysis_id"]) == 64


def test_cli_refuses_to_overwrite_existing_research(tmp_path, capsys):
    output = tmp_path / "evidence.json"
    cli.cmd_capital_audit(None, options(output=str(output)))
    assert json.loads(output.read_text())["stress_before"]["ranked_scenarios"]
    capsys.readouterr()
    with pytest.raises(MultiAssetError, match="refusing to overwrite"):
        cli.cmd_capital_audit(None, options(output=str(output)))


def test_cli_missing_manifest_fails_closed(tmp_path):
    with pytest.raises(MultiAssetError, match="cannot read portfolio manifest"):
        cli.cmd_capital_audit(None, options(manifest=str(tmp_path / "missing.json")))
