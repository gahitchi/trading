"""Selected risk styles only narrow original hard limits; preview does not trade."""

import itertools
from dataclasses import dataclass, replace

import pytest

from lab.risk_profiles import RiskProfile, RiskProfileError, resolve_profile


@dataclass(frozen=True)
class Risk:
    max_weight: float = 0.25
    max_gross: float = 1.0
    max_drawdown: float = 0.35
    max_short_gross: float = 0.0
    min_net: float = 0.0
    max_net: float = 1.0
    profile: str = "balanced"


@dataclass(frozen=True)
class Paper:
    capital: float = 100000.0
    max_order_value: float = 30000.0
    max_orders_per_day: int = 40
    max_daily_turnover: float = 1.0


@dataclass(frozen=True)
class Portfolio:
    target_vol: float = 0.0


@dataclass(frozen=True)
class Config:
    risk: Risk = Risk()
    paper: Paper = Paper()
    portfolio: Portfolio = Portfolio()


def test_profiles_monotonically_expand_only_within_original_safety_caps():
    baseline = Config()
    profiles = []
    for profile in RiskProfile:
        effective, evidence = resolve_profile(baseline, profile.value)
        assert evidence["hard_caps_relaxed"] is False
        assert evidence["status"] == "PREVIEW_ONLY_NOT_AN_EXECUTION_PERMIT"
        assert evidence["paper_execution_permissions"] == {"short_selling": False, "margin_leverage": False}
        assert effective.risk.max_gross <= 1.0
        assert effective.risk.max_weight <= baseline.risk.max_weight
        assert effective.risk.max_drawdown <= baseline.risk.max_drawdown
        assert effective.paper.max_order_value <= baseline.paper.max_order_value
        assert effective.paper.max_orders_per_day <= baseline.paper.max_orders_per_day
        assert effective.paper.max_daily_turnover <= baseline.paper.max_daily_turnover
        profiles.append(evidence["limits"])
    for earlier, later in itertools.pairwise(profiles):
        assert all(earlier[k] <= later[k] for k in earlier)
    assert profiles[0]["max_gross"] < profiles[-1]["max_gross"]
    assert baseline == Config()


def test_aggressive_never_relaxes_preexisting_tighter_operator_limits():
    baseline = Config(
        risk=Risk(0.05, 0.18, 0.04),
        paper=Paper(20000.0, 500.0, 2, 0.08),
        portfolio=Portfolio(0.03),
    )
    effective, report = resolve_profile(baseline, "aggressive")
    assert effective.risk.max_weight == baseline.risk.max_weight
    assert effective.risk.max_gross == baseline.risk.max_gross
    assert effective.risk.max_drawdown == baseline.risk.max_drawdown
    assert effective.risk.max_net == baseline.risk.max_gross
    assert effective.paper == baseline.paper
    assert effective.portfolio == baseline.portfolio
    assert len(report["profile_id"]) == 64


def test_invalid_config_values_and_typos_fail_closed():
    with pytest.raises(RiskProfileError, match="unknown"):
        resolve_profile(Config(), "reckless")
    with pytest.raises(RiskProfileError, match="finite"):
        resolve_profile(Config(risk=replace(Risk(), max_drawdown=float("nan"))), "calm")
    with pytest.raises(RiskProfileError, match="positive"):
        resolve_profile(Config(paper=replace(Paper(), capital=0)), "calm")
    with pytest.raises(RiskProfileError, match="nonnegative"):
        resolve_profile(Config(portfolio=Portfolio(-0.5)), "calm")


def test_same_profile_and_settings_produce_same_sha256():
    a = resolve_profile(Config(), "dynamic")[1]
    b = resolve_profile(Config(), "dynamic")[1]
    c = resolve_profile(Config(), "calm")[1]
    assert a == b
    assert a["profile_id"] != c["profile_id"]


def test_cli_selectable_preview_has_no_order_side_effects(capsys):
    from lab.cli import build_parser, cmd_risk_profile

    args = build_parser().parse_args(["risk-profile", "--profile", "cautious", "--json"])
    assert args.fn is cmd_risk_profile
    cmd_risk_profile(Config(), args)
    assert '"profile": "cautious"' in capsys.readouterr().out


def test_advanced_capabilities_require_separate_broker_authorization():
    safe = ("calm", "cautious", "balanced")
    for name in safe:
        report = resolve_profile(Config(), name)[1]
        assert not report["research_capability_eligibility"]["short_selling"]
        assert not report["research_capability_eligibility"]["margin_leverage"]
    for name in ("dynamic", "aggressive"):
        report = resolve_profile(Config(), name)[1]
        assert report["research_capability_eligibility"]["short_selling"]
        assert report["research_capability_eligibility"]["margin_leverage"]
    for name in RiskProfile:
        report = resolve_profile(Config(), name.value)[1]
        assert report["paper_execution_permissions"] == {"short_selling": False, "margin_leverage": False}


def test_explicit_higher_operator_caps_enable_signed_profile_preview_only():
    wide = Config(
        risk=Risk(
            max_weight=0.5,
            max_gross=1.9,
            max_drawdown=0.4,
            max_short_gross=0.9,
            min_net=-1.0,
            max_net=1.9,
        ),
    )
    _, safe = resolve_profile(wide, "calm")
    _, dynamic = resolve_profile(wide, "dynamic")
    _, aggressive = resolve_profile(wide, "aggressive")
    assert safe["limits"]["max_gross"] == 0.25
    assert safe["limits"]["max_short_gross"] == 0
    assert dynamic["limits"]["max_gross"] == 1.35
    assert dynamic["limits"]["max_short_gross"] == 0.30
    assert dynamic["limits"]["min_net"] == -0.35
    assert aggressive["limits"]["max_gross"] == 1.8
    assert aggressive["limits"]["max_short_gross"] == 0.75
    assert aggressive["limits"]["min_net"] == -0.75
    for report in (safe, dynamic, aggressive):
        assert report["paper_execution_permissions"] == {"short_selling": False, "margin_leverage": False}
    assert dynamic["requires_margin_core_qualification"]
    assert aggressive["profile_id"] != dynamic["profile_id"]


def test_profile_does_not_relax_mandatory_operator_short_limits():
    baseline = Config(
        risk=Risk(max_gross=1.1, max_short_gross=0.12, min_net=-0.05, max_net=0.8),
    )
    effective, _ = resolve_profile(baseline, "aggressive")
    assert effective.risk.max_gross == 1.1
    assert effective.risk.max_short_gross == 0.12
    assert effective.risk.min_net == -0.05
    assert effective.risk.max_net == 0.8


def test_impossible_signed_operator_envelope_fails_closed():
    with pytest.raises(RiskProfileError, match="operator short/net"):
        resolve_profile(Config(risk=replace(Risk(), max_short_gross=2)), "aggressive")
    with pytest.raises(RiskProfileError, match="operator short/net"):
        resolve_profile(Config(risk=replace(Risk(), min_net=0.7, max_net=0.2)), "calm")
