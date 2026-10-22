"""Risk-budget profiles as deterministic bounds over operator-configured hard limits.

This module never authorizes a short, margin account or real-money order. Each
profile caps a validated operator envelope, and specifies a volatility budget
for the existing AllocatorAgent; the broker/risk agents remain the final gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from lab.config import ConfigError, PortfolioSettings, RiskSettings


@dataclass(frozen=True, slots=True)
class Profile:
    target_vol: float
    max_drawdown: float
    max_gross: float
    max_short_gross: float
    max_weight: float
    min_net: float
    max_net: float
    max_leverage: float


PROFILES: dict[str, Profile] = {
    "calm": Profile(0.06, 0.08, 0.30, 0.0, 0.08, 0.0, 0.30, 1.0),
    "cautious": Profile(0.10, 0.12, 0.50, 0.0, 0.12, 0.0, 0.50, 1.0),
    "balanced": Profile(0.14, 0.18, 1.00, 0.0, 0.25, 0.0, 1.00, 1.0),
    "dynamic": Profile(0.20, 0.25, 1.35, 0.30, 0.30, -0.30, 1.10, 1.35),
    "aggressive": Profile(0.28, 0.30, 1.80, 0.75, 0.45, -0.75, 1.60, 1.80),
}


def apply_profile(
    risk: RiskSettings, portfolio: PortfolioSettings, profile: str
) -> tuple[RiskSettings, PortfolioSettings]:
    """Intersect a profile with already-validated operator limits.

    A zero operator target_vol means 'not explicitly capped' when selecting a
    preset, so the preset turns risk-unit volatility targeting on. An explicit
    positive operator target is a tighter cap; it may not be raised by a preset.
    A custom profile leaves the preexisting configuration completely unchanged.
    """
    if profile == "custom":
        return risk, portfolio
    if profile not in PROFILES:
        raise ConfigError(f"risk.profile {profile!r}: choose custom or {', '.join(PROFILES)}")
    p = PROFILES[profile]
    if not math.isfinite(portfolio.target_vol) or portfolio.target_vol < 0:
        raise ConfigError("portfolio.target_vol must be finite and nonnegative")
    gross = min(risk.max_gross, p.max_gross)
    short = min(risk.max_short_gross, p.max_short_gross, gross)
    min_net = max(risk.min_net, p.min_net, -short)
    max_net = min(risk.max_net, p.max_net, gross)
    if min_net > max_net:
        raise ConfigError(
            f"risk.profile {profile!r}: operator net limits conflict with profile; "
            "change the hard limits or select custom"
        )
    effective_risk = replace(
        risk,
        profile=profile,
        max_weight=min(risk.max_weight, p.max_weight),
        max_gross=gross,
        max_short_gross=short,
        min_net=min_net,
        max_net=max_net,
        max_drawdown=min(risk.max_drawdown, p.max_drawdown),
    )
    vol = min(portfolio.target_vol, p.target_vol) if portfolio.target_vol > 0 else p.target_vol
    ceiling = min(portfolio.max_leverage, p.max_leverage, gross)
    effective_portfolio = replace(
        portfolio,
        target_vol=vol,
        max_leverage=ceiling,
        leverage=min(portfolio.leverage, ceiling),
    )
    return effective_risk, effective_portfolio
