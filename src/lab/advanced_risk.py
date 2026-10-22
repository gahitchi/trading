"""Research-only short/leverage stress envelope.

This is a scenario validator, NOT an IBKR margin calculator or an execution
authorization. It deliberately has no broker client, strategy, or order APIs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal


class AdvancedRiskError(ValueError):
    """Risk scenario is unsupported, unverified, or internally inconsistent."""


def _num(v: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(v, Decimal) or not v.is_finite():
        raise AdvancedRiskError(f"{name}: finite Decimal required")
    if (positive and v <= 0) or (not positive and v < 0):
        raise AdvancedRiskError(f"{name}: {'positive' if positive else 'nonnegative'} required")
    return v


@dataclass(frozen=True, slots=True)
class MarginInstrument:
    instrument_id: str
    bid: Decimal
    ask: Decimal
    short_locate_confirmed: bool = False
    borrow_fee_apr: Decimal | None = None

    def __post_init__(self):
        if not self.instrument_id or self.instrument_id.strip() != self.instrument_id:
            raise AdvancedRiskError("permanent instrument identifier required")
        _num(self.bid, "bid", positive=True)
        _num(self.ask, "ask", positive=True)
        if self.ask < self.bid:
            raise AdvancedRiskError("crossed market bid/ask")
        if self.borrow_fee_apr is not None:
            _num(self.borrow_fee_apr, "borrow_fee_apr")
        if self.short_locate_confirmed and self.borrow_fee_apr is None:
            raise AdvancedRiskError("short locate requires explicit borrow-fee evidence")


@dataclass(frozen=True, slots=True)
class MarginPosition:
    instrument_id: str
    quantity: Decimal  # positive long, negative short

    def __post_init__(self):
        if not self.instrument_id or not isinstance(self.quantity, Decimal) or not self.quantity.is_finite():
            raise AdvancedRiskError("signed quantity must be finite Decimal")


@dataclass(frozen=True, slots=True)
class AdvancedRiskPolicy:
    max_gross_leverage: Decimal
    max_short_gross_fraction: Decimal
    initial_margin_fraction: Decimal
    maintenance_margin_fraction: Decimal
    max_stress_loss_fraction: Decimal
    allow_short_research: bool = False
    allow_margin_research: bool = False

    def __post_init__(self):
        for label in (
            "max_gross_leverage", "max_short_gross_fraction", "initial_margin_fraction",
            "maintenance_margin_fraction", "max_stress_loss_fraction",
        ):
            _num(getattr(self, label), label, positive=label == "max_gross_leverage")
        if not Decimal(0) < self.maintenance_margin_fraction <= self.initial_margin_fraction <= Decimal(1):
            raise AdvancedRiskError("maintenance margin must be <= initial margin, both in (0,1]")
        if self.max_gross_leverage > Decimal(2):
            raise AdvancedRiskError("research margin model capped at 2x gross leverage")
        if self.max_short_gross_fraction > Decimal(1) or self.max_stress_loss_fraction > Decimal(1):
            raise AdvancedRiskError("short budget and stress loss cap must be <= 100%")


def evaluate_margin_scenario(
    *,
    cash_after_financing_and_short_proceeds: Decimal,
    positions: tuple[MarginPosition, ...],
    instruments: dict[str, MarginInstrument],
    policy: AdvancedRiskPolicy,
    shock_returns: dict[str, Decimal],
    holding_days: int = 1,
    margin_loan: Decimal = Decimal("0"),
    margin_loan_fee_apr: Decimal | None = None,
) -> dict:
    """Adverse bid/ask marks, explicit short borrow and hypothetical margin calls.

    Cash includes documented short sale proceeds and any borrowed cash. All
    financial costs and cash movements must be supplied externally; the
    approximation is not a broker-approved buying-power or settlement model.
    """
    if not isinstance(policy, AdvancedRiskPolicy):
        raise AdvancedRiskError("typed margin policy required")
    _num(cash_after_financing_and_short_proceeds, "cash")
    _num(margin_loan, "margin_loan")
    if margin_loan > 0 and (not policy.allow_margin_research or margin_loan_fee_apr is None):
        raise AdvancedRiskError("margin loan requires explicit research permission and financing rate")
    if margin_loan_fee_apr is not None:
        _num(margin_loan_fee_apr, "margin_loan_fee_apr")
    if type(holding_days) is not int or not 1 <= holding_days <= 365:
        raise AdvancedRiskError("holding_days must be an integer between 1 and 365")
    if not positions or len(positions) > 200:
        raise AdvancedRiskError("1–200 positions required")
    if set(shock_returns) != {p.instrument_id for p in positions}:
        raise AdvancedRiskError("stress scenario must explicitly price every position")
    unique = set()
    long_value = short_liability = stressed_long = stressed_short = Decimal(0)
    borrow_cost = Decimal(0)
    exposure = []
    for p in positions:
        if p.instrument_id in unique:
            raise AdvancedRiskError("duplicate instrument position")
        unique.add(p.instrument_id)
        market = instruments.get(p.instrument_id)
        if not isinstance(market, MarginInstrument):
            raise AdvancedRiskError(f"{p.instrument_id}: no verified quote metadata")
        shock = shock_returns[p.instrument_id]
        if not isinstance(shock, Decimal) or not shock.is_finite() or shock <= Decimal("-1"):
            raise AdvancedRiskError("stress shock must be a finite return greater than -100%")
        if shock > Decimal(10):
            raise AdvancedRiskError("maximum stress shock bounded to +1000% for this research model")
        if p.quantity < 0:
            if not policy.allow_short_research or not market.short_locate_confirmed:
                raise AdvancedRiskError(f"{p.instrument_id}: short permissions/locate unavailable")
            if market.borrow_fee_apr is None:
                raise AdvancedRiskError("missing short borrow fee")
            short_liability += -p.quantity * market.ask
            stressed_short += -p.quantity * market.ask * (Decimal(1) + shock)
            borrow_cost += -p.quantity * market.ask * market.borrow_fee_apr * Decimal(holding_days) / Decimal(365)
        else:
            long_value += p.quantity * market.bid
            stressed_long += p.quantity * market.bid * (Decimal(1) + shock)
        exposure.append({
            "instrument": p.instrument_id,
            "side": "SHORT" if p.quantity < 0 else "LONG",
            "quantity": str(p.quantity),
            "scenario_return": str(shock),
        })
    equity = cash_after_financing_and_short_proceeds + long_value - short_liability - margin_loan
    if equity <= 0:
        raise AdvancedRiskError("non-positive equity cannot authorize scenario analysis")
    gross = long_value + short_liability
    stressed_gross = stressed_long + stressed_short
    finance_cost = (
        margin_loan * margin_loan_fee_apr * Decimal(holding_days) / Decimal(365)
        if margin_loan > 0 else Decimal(0)
    )
    stressed_equity = cash_after_financing_and_short_proceeds + stressed_long - stressed_short - margin_loan
    stressed_equity -= borrow_cost + finance_cost
    leverage = gross / equity
    short_fraction = short_liability / equity
    if gross > equity and not policy.allow_margin_research:
        raise AdvancedRiskError("margin research permission is not enabled")
    loss_fraction = (equity - stressed_equity) / equity
    reasons = []
    if leverage > policy.max_gross_leverage:
        reasons.append("GROSS_LEVERAGE_LIMIT")
    if short_fraction > policy.max_short_gross_fraction:
        reasons.append("SHORT_BUDGET_LIMIT")
    if equity < gross * policy.initial_margin_fraction:
        reasons.append("INITIAL_MARGIN_INSUFFICIENT")
    if stressed_equity < stressed_gross * policy.maintenance_margin_fraction:
        reasons.append("STRESS_MAINTENANCE_MARGIN_BREACH")
    if loss_fraction > policy.max_stress_loss_fraction:
        reasons.append("STRESS_LOSS_LIMIT")
    # A lack of a triggered condition in THIS scenario is not proof of
    # broker eligibility, funding, financing or risk qualification.
    manifest = {
        "schema_version": 1,
        "qualification": "RESEARCH_ONLY_NO_BROKER_PERMISSIONS",
        "pre_stress_equity": str(equity),
        "pre_stress_gross": str(gross),
        "pre_stress_gross_leverage": str(leverage),
        "pre_stress_short_fraction": str(short_fraction),
        "estimated_borrow_cost": str(borrow_cost),
        "margin_loan_principal": str(margin_loan),
        "estimated_loan_interest": str(finance_cost),
        "stressed_equity": str(stressed_equity),
        "stressed_gross": str(stressed_gross),
        "stress_loss_fraction": str(loss_fraction),
        "breached_guards": reasons,
        "can_submit_orders": False,
        "positions": exposure,
        "warnings": [
            "Standalone scenario, not broker buying power or settlement",
            "Borrow availability, margin, dynamic liquidation and nonlinear fees require broker evidence",
            "Short losses are theoretically unbounded; this finite shock set does not bound tail risk",
        ],
    }
    manifest["analysis_id"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return manifest
