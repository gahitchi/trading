"""Audited, counterfactual multi-asset portfolio stress analysis."""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from lab.multiasset import MultiAssetError
from lab.multiasset_report import analyze_manifest, load_manifest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "multiasset-offline" / "portfolio.json"


def source():
    return load_manifest(EXAMPLE)


def test_comprehensive_capital_scenario_is_reproducible_with_exact_components():
    manifest = source()
    first = analyze_manifest(manifest)
    assert first == analyze_manifest(manifest)
    assert first["qualification"] == "RESEARCH_ONLY_NOT_FOR_TRADING"
    assert first["asset_classes"] == ["BOND", "EQUITY", "FX_SPOT"]
    assert Decimal(first["liquidation_mark_before"]) == Decimal("23475")
    assert len(first["analysis_id"]) == 64
    before = first["stress_before"]
    after = first["stress_after_spot_previews"]
    assert before["risk_status"] == "LIMIT_BREACH_IN_ASSUMED_SCENARIOS"
    assert before["worst_case_name"] == "severe_combined"
    assert after["worst_case_name"] == "severe_combined"
    assert Decimal(first["liquidation_mark_after_spot_previews"]) < Decimal(first["liquidation_mark_before"])
    for stress in (before, after):
        assert stress["analysis_id"]
        for scenario in stress["ranked_scenarios"]:
            total_delta = sum((Decimal(x) for x in scenario["component_pnl"].values()), Decimal(0))
            assert total_delta == Decimal(scenario["pnl"])
            assert Decimal(scenario["scenario_value"]) - Decimal(stress["baseline_value"]) == total_delta
            assert scenario["factor_trace"]
    assert first["cash_after_spot_previews"]["EUR"] == "3000"
    assert Decimal(first["cash_after_spot_previews"]["USD"]) == Decimal("8897.80000")


def test_changed_assumptions_change_evidence_id_and_losses():
    old = source()
    changed = deepcopy(old)
    changed["scenarios"][0]["factors"]["rate_parallel"] = "0.02"
    a, b = analyze_manifest(old), analyze_manifest(changed)
    assert a["analysis_id"] != b["analysis_id"]
    assert a["stress_before"]["analysis_id"] != b["stress_before"]["analysis_id"]


def test_missing_factor_model_evidence_rejected():
    changed = source()
    changed["scenarios"][0]["factors"]["not_observed"] = "0.25"
    with pytest.raises(MultiAssetError, match="lacks sensitivity evidence"):
        analyze_manifest(changed)


def test_conflicting_direct_and_factor_shocks_rejected():
    changed = source()
    changed["scenarios"][0]["direct_shocks"] = {"BOND-ISIN": "-0.1"}
    with pytest.raises(MultiAssetError, match="ambiguous precedence"):
        analyze_manifest(changed)


def test_duplicate_hypotheses_are_rejected():
    changed = source()
    changed["scenarios"][1]["name"] = changed["scenarios"][0]["name"]
    with pytest.raises(MultiAssetError, match="duplicate scenario"):
        analyze_manifest(changed)


def test_unbounded_shock_and_negative_cash_rejected():
    changed = source()
    changed["scenarios"][0]["factors"]["rate_parallel"] = "-1"
    with pytest.raises(MultiAssetError):
        analyze_manifest(changed)
    changed = source()
    changed["cash"]["USD"] = "-1"
    with pytest.raises(MultiAssetError, match="nonnegative"):
        analyze_manifest(changed)


def test_empty_scenarios_with_limits_never_appear_safe():
    changed = source()
    changed["scenarios"] = []
    with pytest.raises(MultiAssetError, match="1-64"):
        analyze_manifest(changed)
    changed = source()
    del changed["scenarios"]
    del changed["factor_exposures"]
    del changed["stress_policy"]
    report = analyze_manifest(changed)
    assert report["stress_before"] is None
    assert report["stress_after_spot_previews"] is None


def test_stale_marks_fail_before_any_scenario_analysis():
    changed = source()
    changed["prices"][0]["timestamp"] = "2026-10-22T13:00:00+00:00"
    with pytest.raises(MultiAssetError, match="stale"):
        analyze_manifest(changed)


def test_duplicate_manifest_keys_are_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(MultiAssetError, match="duplicate JSON"):
        load_manifest(path)


def test_scenario_engine_imports_no_broker_tools():
    from lab import multiasset, multiasset_report, stress_reasoning

    for module in (multiasset, multiasset_report, stress_reasoning):
        for forbidden in ("submit_order", "InteractiveBrokersClient", "BrokerClient"):
            assert forbidden not in vars(module)
