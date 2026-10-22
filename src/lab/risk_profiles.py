"""Bounded, selectable research/paper risk budgets; never broker authorization."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lab.config import LabConfig


class RiskProfileError(ValueError):
    """An invalid operator preference or unsafe configured hard limit."""


class RiskProfile(StrEnum):
    CALM = "calm"
    CAUTIOUS = "cautious"
    BALANCED = "balanced"
    DYNAMIC = "dynamic"
    AGGRESSIVE = "aggressive"


@dataclass(frozen=True, slots=True)
class RiskBudget:
    max_gross: float
    max_weight: float
    max_drawdown: float
    target_vol: float
    order_fraction: float
    daily_turnover: float
    daily_orders: int
    max_short_gross: float = 0.0
    min_net: float = 0.0
    max_net: float = 1.0


BUDGETS: dict[RiskProfile, RiskBudget] = {
    RiskProfile.CALM: RiskBudget(0.25, 0.08, 0.08, 0.08, 0.04, 0.10, 8, max_net=0.25),
    RiskProfile.CAUTIOUS: RiskBudget(0.40, 0.10, 0.12, 0.12, 0.06, 0.20, 12, max_net=0.40),
    RiskProfile.BALANCED: RiskBudget(0.75, 0.15, 0.18, 0.15, 0.10, 0.35, 20, max_net=0.75),
    RiskProfile.DYNAMIC: RiskBudget(
        1.35, 0.20, 0.25, 0.20, 0.15, 0.60, 30, max_short_gross=0.30, min_net=-0.35, max_net=1.10
    ),
    RiskProfile.AGGRESSIVE: RiskBudget(
        1.80, 0.30, 0.30, 0.25, 0.20, 1.00, 40, max_short_gross=0.75, min_net=-0.75, max_net=1.60
    ),
}


def _positive(x, name: str) -> float:
    try:
        value = float(x)
    except (TypeError, ValueError) as exc:
        raise RiskProfileError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RiskProfileError(f"{name} must be a finite positive number")
    return value


def _finite(x, name: str) -> float:
    try:
        value = float(x)
    except (TypeError, ValueError) as exc:
        raise RiskProfileError(f"{name} must be finite") from exc
    if not math.isfinite(value):
        raise RiskProfileError(f"{name} must be finite")
    return value


def resolve_profile(cfg: LabConfig, name: str) -> tuple[LabConfig, dict]:
    """Compute deterministic narrowed limits; preserves every configured hard cap.

    A profile sets exposure bounds, not market signals or broker permissions.
    The caller must independently qualify any paper-trading launch.
    """
    try:
        profile = RiskProfile(name)
    except ValueError as exc:
        raise RiskProfileError(f"unknown profile {name!r}; choose {[x.value for x in RiskProfile]}") from exc
    b = BUDGETS[profile]
    for key in ("max_weight", "max_gross", "max_drawdown"):
        _positive(getattr(cfg.risk, key), f"risk.{key}")
    for key in ("capital", "max_order_value", "max_daily_turnover"):
        _positive(getattr(cfg.paper, key), f"paper.{key}")
    if type(cfg.paper.max_orders_per_day) is not int or cfg.paper.max_orders_per_day <= 0:
        raise RiskProfileError("paper.max_orders_per_day must be a positive integer")
    old_vol = float(cfg.portfolio.target_vol)
    if not math.isfinite(old_vol) or old_vol < 0:
        raise RiskProfileError("portfolio.target_vol must be nonnegative and finite")
    hard_gross = float(cfg.risk.max_gross)
    hard_short = _finite(getattr(cfg.risk, "max_short_gross", 0.0), "risk.max_short_gross")
    hard_min_net = _finite(getattr(cfg.risk, "min_net", 0.0), "risk.min_net")
    hard_max_net = _finite(getattr(cfg.risk, "max_net", 1.0), "risk.max_net")
    if not (0 <= hard_short <= hard_gross and -hard_gross <= hard_min_net <= hard_max_net <= hard_gross):
        raise RiskProfileError("operator short/net limits must fit within configured gross exposure")
    gross = min(hard_gross, b.max_gross)
    min_net = max(hard_min_net, b.min_net, -gross)
    max_net = min(hard_max_net, b.max_net, gross)
    if min_net > max_net:
        raise RiskProfileError("profile net limits conflict with operator hard limits")
    risk = replace(
        cfg.risk,
        max_weight=min(cfg.risk.max_weight, b.max_weight),
        max_gross=gross,
        max_drawdown=min(cfg.risk.max_drawdown, b.max_drawdown),
        max_short_gross=min(hard_short, b.max_short_gross, gross),
        min_net=min_net,
        max_net=max_net,
        profile=profile.value,
    )
    paper = replace(
        cfg.paper,
        max_order_value=min(cfg.paper.max_order_value, b.order_fraction * cfg.paper.capital),
        max_orders_per_day=min(cfg.paper.max_orders_per_day, b.daily_orders),
        max_daily_turnover=min(cfg.paper.max_daily_turnover, b.daily_turnover),
    )
    portfolio = replace(
        cfg.portfolio,
        target_vol=min(old_vol, b.target_vol) if old_vol > 0 else b.target_vol,
    )
    effective = replace(cfg, risk=risk, paper=paper, portfolio=portfolio)
    limits = {
        "max_gross": risk.max_gross,
        "max_weight": risk.max_weight,
        "max_drawdown": risk.max_drawdown,
        "max_short_gross": risk.max_short_gross,
        "min_net": risk.min_net,
        "max_net": risk.max_net,
        "target_vol": portfolio.target_vol,
        "max_order_value": paper.max_order_value,
        "max_orders_per_day": paper.max_orders_per_day,
        "max_daily_turnover": paper.max_daily_turnover,
    }
    fingerprint = hashlib.sha256(
        json.dumps({"name": profile.value, "limits": limits}, sort_keys=True).encode()
    ).hexdigest()
    research_capabilities = {
        "short_selling": profile in (RiskProfile.DYNAMIC, RiskProfile.AGGRESSIVE),
        "margin_leverage": profile in (RiskProfile.DYNAMIC, RiskProfile.AGGRESSIVE),
        "simulation_only_short": profile == RiskProfile.BALANCED,
    }
    return effective, {
        "schema_version": 1,
        "profile": profile.value,
        "research_capability_eligibility": research_capabilities,
        "paper_execution_permissions": {"short_selling": False, "margin_leverage": False},
        "status": "PREVIEW_ONLY_NOT_AN_EXECUTION_PERMIT",
        "requires_margin_core_qualification": gross > 1.0 or risk.max_short_gross > 0,
        "limits": limits,
        "profile_id": fingerprint,
        "hard_caps_relaxed": False,
        "warning": "Profile caps require separately verified broker borrow, margin, and execution controls; "
        "preview never authorizes orders or bypasses paper-only safety.",
    }
