"""Selected risk styles only narrow original hard limits; preview does not trade."""

from dataclasses import dataclass, replace

import pytest

from lab.risk_profiles import RiskProfile, RiskProfileError, resolve_profile


@dataclass(frozen=True)
class Risk:
    max_weight: float = 0.25
    max_gross: float = 1.0
    max_drawdown: float = 0.35


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
        assert effective.risk.max_gross <= 1.0
        assert effective.risk.max_weight <= baseline.risk.max_weight
        assert effective.risk.max_drawdown <= baseline.risk.max_drawdown
        assert effective.paper.max_order_value <= baseline.paper.max_order_value
        assert effective.paper.max_orders_per_day <= baseline.paper.max_orders_per_day
        assert effective.paper.max_daily_turnover <= baseline.paper.max_daily_turnover
        profiles.append(evidence["limits"])
    for earlier, later in zip(profiles, profiles[1:], strict=False):
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
    assert effective.risk == baseline.risk
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
