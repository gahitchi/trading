"""Bounded, selectable research/paper risk budgets; never broker authorization."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
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


BUDGETS: dict[RiskProfile, RiskBudget] = {
    RiskProfile.CALM: RiskBudget(0.25, 0.08, 0.08, 0.08, 0.04, 0.10, 8),
    RiskProfile.CAUTIOUS: RiskBudget(0.40, 0.10, 0.12, 0.12, 0.06, 0.20, 12),
    RiskProfile.BALANCED: RiskBudget(0.60, 0.15, 0.18, 0.15, 0.10, 0.35, 20),
    RiskProfile.DYNAMIC: RiskBudget(0.80, 0.20, 0.25, 0.20, 0.15, 0.60, 30),
    RiskProfile.AGGRESSIVE: RiskBudget(1.00, 0.25, 0.30, 0.25, 0.20, 1.00, 40),
}


def _positive(x, name: str) -> float:
    try:
        value = float(x)
    except (TypeError, ValueError) as exc:
        raise RiskProfileError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RiskProfileError(f"{name} must be a finite positive number")
    return value


def resolve_profile(cfg: LabConfig, name: str) -> tuple[LabConfig, dict]:
    """Compute deterministic narrowed limits; preserves every configured hard cap.

    A profile changes permissible exposure, not market signals or the safety
    guards. The caller must independently qualify any paper-trading launch.
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
    risk = replace(
        cfg.risk,
        max_weight=min(cfg.risk.max_weight, b.max_weight),
        max_gross=min(cfg.risk.max_gross, b.max_gross, 1.0),
        max_drawdown=min(cfg.risk.max_drawdown, b.max_drawdown),
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
        "target_vol": portfolio.target_vol,
        "max_order_value": paper.max_order_value,
        "max_orders_per_day": paper.max_orders_per_day,
        "max_daily_turnover": paper.max_daily_turnover,
    }
    fingerprint = hashlib.sha256(json.dumps({"name": profile.value, "limits": limits}, sort_keys=True).encode()).hexdigest()
    return effective, {
        "schema_version": 1,
        "profile": profile.value,
        "status": "PREVIEW_ONLY_NOT_AN_EXECUTION_PERMIT",
        "limits": limits,
        "profile_id": fingerprint,
        "hard_caps_relaxed": False,
        "warning": "Profiles do not override safety halts, paper-only accounts or broker reconciliation.",
    }
