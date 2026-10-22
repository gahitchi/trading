"""Profiles are real configuration controls, not preview-only labels."""

from dataclasses import replace

import pytest

from lab.config import ConfigError, PortfolioSettings, RiskSettings, load_config
from lab.risk_profiles import PROFILES, apply_profile


def _wide():
    return RiskSettings(
        max_weight=0.50,
        max_gross=1.90,
        max_drawdown=0.40,
        max_short_gross=0.90,
        min_net=-0.90,
        max_net=1.80,
    )


def _portfolio():
    return PortfolioSettings(target_vol=0.0, vol_window=63, max_leverage=1.80, leverage=1.0)


@pytest.mark.parametrize("name", list(PROFILES))
def test_presets_are_real_volatility_budgets_and_bounded_by_operator(name):
    r, p = apply_profile(_wide(), _portfolio(), name)
    spec = PROFILES[name]
    assert r.profile == name
    assert r.max_gross == pytest.approx(min(1.90, spec.max_gross))
    assert r.max_short_gross == pytest.approx(spec.max_short_gross)
    assert r.max_weight == pytest.approx(spec.max_weight)
    assert r.max_drawdown == pytest.approx(spec.max_drawdown)
    assert p.target_vol == pytest.approx(spec.target_vol)
    assert p.max_leverage == pytest.approx(min(1.8, spec.max_leverage, r.max_gross))
    assert r.min_net >= _wide().min_net
    assert r.max_net <= _wide().max_net


def test_operator_hard_limits_remain_absolute():
    r = replace(_wide(), max_gross=0.70, max_short_gross=0.06, min_net=0, max_net=0.70)
    p = replace(_portfolio(), target_vol=0.045, max_leverage=0.70, leverage=0.70)
    for name in PROFILES:
        effective, q = apply_profile(r, p, name)
        assert effective.max_gross <= r.max_gross
        assert effective.max_short_gross <= r.max_short_gross
        assert effective.max_weight <= r.max_weight
        assert effective.max_drawdown <= r.max_drawdown
        assert q.target_vol <= p.target_vol
        assert q.max_leverage <= p.max_leverage
        assert 0 <= effective.max_short_gross <= effective.max_gross


def test_custom_is_a_true_backward_compatible_no_op():
    r, p = _wide(), _portfolio()
    assert apply_profile(r, p, "custom") == (r, p)


def test_invalid_or_inconsistent_envelopes_fail_closed():
    with pytest.raises(ConfigError, match="choose custom"):
        apply_profile(_wide(), _portfolio(), "reckless")
    with pytest.raises(ConfigError, match="operator net limits"):
        apply_profile(replace(_wide(), min_net=1.0, max_net=1.2), _portfolio(), "calm")
    with pytest.raises(ConfigError, match="finite"):
        apply_profile(_wide(), replace(_portfolio(), target_vol=float("nan")), "dynamic")


def test_risk_profile_is_applied_by_real_config_loader(goog_lab, tmp_path):
    # Test the exact route paper.build_node and the backtest both consume.
    root = goog_lab.root
    src = (root / "lab.toml").read_text()
    assert 'profile = "custom"' not in src or "profile" in src
    modified = src.replace('profile = "custom"', 'profile = "calm"')
    path = tmp_path / "lab.toml"
    path.write_text(modified)
    loaded = load_config(path)
    assert loaded.risk.profile == "calm"
    assert loaded.risk.max_gross <= 0.30
    assert loaded.risk.max_drawdown <= 0.08
    assert loaded.risk.max_short_gross == 0
    assert loaded.portfolio.target_vol == pytest.approx(0.06)
    assert loaded.portfolio.max_leverage <= 0.30


def test_cli_reports_actual_effective_profile(goog_lab, capsys):
    from lab.cli import build_parser, cmd_risk_profile

    args = build_parser().parse_args(["risk-profile", "--json"])
    assert args.fn is cmd_risk_profile
    assert cmd_risk_profile(goog_lab, args) == 0
    import json

    report = json.loads(capsys.readouterr().out)
    assert report["profile"] == "custom"
    assert report["risk"]["max_gross"] == goog_lab.risk.max_gross
    assert report["portfolio"]["target_vol"] == goog_lab.portfolio.target_vol
    assert not report["broker_shortability_verified"]
    assert not report["real_money_authorized"]


@pytest.mark.parametrize("risk_style,expected_short", [("calm", False), ("aggressive", True)])
def test_selected_profile_flows_to_real_paper_node(goog_lab, goog, tmp_path, risk_style, expected_short):
    """End-to-end: actual LiveNode with the signed core and a simulated paper broker."""
    from lab.config import load_config
    from lab.signals.registry import SignalSpec
    from lab.sim.harness import LiveHarness

    config_text = (goog_lab.root / "lab.toml").read_text()
    config_text = config_text.replace('profile = "custom"', f'profile = "{risk_style}"')
    if risk_style == "aggressive":
        config_text = config_text.replace("max_weight = 0.25", "max_weight = 0.45")
        config_text = config_text.replace("max_gross = 1.0", "max_gross = 1.8")
        config_text = config_text.replace("max_short_gross = 0.0", "max_short_gross = 0.75")
        config_text = config_text.replace("min_net = 0.0", "min_net = -0.75")
        config_text = config_text.replace("max_net = 1.0", "max_net = 1.6")
        config_text = config_text.replace("max_leverage = 1.0", "max_leverage = 1.8")
    path = tmp_path / "selected" / "lab.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_text)
    cfg = load_config(path)
    assert cfg.risk.profile == risk_style
    assert cfg.portfolio.target_vol == pytest.approx(0.06 if risk_style == "calm" else 0.28)
    h = LiveHarness(
        cfg,
        [SignalSpec("sma_trend", {"fast": 3, "slow": 10, "short": 1})],
        {"GOOG": goog},
        tmp_path / f"harness-{risk_style}",
        first_day=560,
        allow_short=True,
        shortable={"GOOG"},
    )
    for _ in range(3):
        assert h.session().error is None
    fills = [f for session in h.sessions for f in session.fills]
    assert fills
    position = 0.0
    path = []
    for fill in fills:
        position += fill.qty if fill.side == "BUY" else -fill.qty
        path.append(position)
    if expected_short:
        assert min(path) < 0  # actual net short, not merely a SELL closing a long
        assert cfg.risk.max_short_gross == pytest.approx(0.75)
        assert cfg.portfolio.max_leverage == pytest.approx(1.8)
    else:
        assert min(path) >= 0  # selling a long position is NOT short selling
        assert cfg.risk.max_short_gross == 0.0
        assert cfg.risk.max_gross == pytest.approx(0.30)
    assert h.ledger()["positions"] == h.broker().positions()
