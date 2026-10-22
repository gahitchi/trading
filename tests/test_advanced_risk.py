"""Bounded short/long leverage reasoning without broker order permissions."""

from decimal import Decimal

import pytest

from lab.advanced_risk import (
    AdvancedRiskError,
    AdvancedRiskPolicy,
    MarginInstrument,
    MarginPosition,
    evaluate_margin_scenario,
)

D = Decimal


def policy(*, short=False, margin=False, leverage="1", short_cap="0.5"):
    return AdvancedRiskPolicy(
        max_gross_leverage=D(leverage),
        max_short_gross_fraction=D(short_cap),
        initial_margin_fraction=D("0.5"),
        maintenance_margin_fraction=D("0.3"),
        max_stress_loss_fraction=D("0.2"),
        allow_short_research=short,
        allow_margin_research=margin,
    )


def stock(*, locate=False):
    return {"EQ": MarginInstrument("EQ", D("99.50"), D("100.50"), locate, D("0.15") if locate else None)}


def evaluate(*, cash="20000", positions=None, markets=None, rules=None, shock="1", days=10):
    return evaluate_margin_scenario(
        cash_after_financing_and_short_proceeds=D(cash),
        positions=positions or (MarginPosition("EQ", D("-100")),),
        instruments=markets or stock(locate=True),
        policy=rules or policy(short=True, leverage="2", margin=True, short_cap="1"),
        shock_returns={"EQ": D(shock)},
        holding_days=days,
    )


def test_short_loss_borrow_cost_and_margin_breach():
    report = evaluate()
    assert report["qualification"] == "RESEARCH_ONLY_NO_BROKER_PERMISSIONS"
    assert report["can_submit_orders"] is False
    assert D(report["pre_stress_equity"]) == D("9950")
    assert D(report["pre_stress_gross"]) == D("10050")
    assert D(report["estimated_borrow_cost"]) > 0
    assert D(report["stressed_equity"]) < D(report["pre_stress_equity"])
    assert "STRESS_MAINTENANCE_MARGIN_BREACH" in report["breached_guards"]
    assert "STRESS_LOSS_LIMIT" in report["breached_guards"]
    assert len(report["analysis_id"]) == 64


def test_no_borrow_locate_or_permission_fails_closed():
    with pytest.raises(AdvancedRiskError, match="short permissions"):
        evaluate(rules=policy())
    with pytest.raises(AdvancedRiskError, match="locate"):
        evaluate(markets=stock())
    with pytest.raises(AdvancedRiskError, match="borrow"):
        MarginInstrument("EQ", D("90"), D("91"), True, None)


def test_leverage_requires_explicit_simulation_permission_and_cap():
    positions = (MarginPosition("EQ", D("150")),)
    base = {
        "cash_after_financing_and_short_proceeds": D("5000"),
        "positions": positions,
        "instruments": stock(),
        "shock_returns": {"EQ": D("-0.4")},
        "margin_loan": D("12000"),
        "margin_loan_fee_apr": D("0.1"),
    }
    with pytest.raises(AdvancedRiskError, match="margin loan requires"):
        evaluate_margin_scenario(policy=policy(short=False, margin=False, leverage="2"), **base)
    allowed = evaluate_margin_scenario(
        policy=policy(short=False, margin=True, leverage="2", short_cap="0"),
        **base,
    )
    assert D(allowed["pre_stress_gross_leverage"]) > 1
    assert allowed["can_submit_orders"] is False
    assert D(allowed["estimated_loan_interest"]) > 0
    assert "STRESS_MAINTENANCE_MARGIN_BREACH" in allowed["breached_guards"]


def test_unbounded_short_shocks_are_stressed_not_clipped_at_loss_of_100pct():
    small = evaluate(shock="1")
    extreme = evaluate(shock="5")
    assert D(extreme["stressed_equity"]) < D(small["stressed_equity"])
    assert D(extreme["stress_loss_fraction"]) > 1
    assert extreme["can_submit_orders"] is False


def test_nonfinite_prices_unsupported_shocks_duplicate_positions_fail_closed():
    with pytest.raises(AdvancedRiskError, match="finite"):
        MarginInstrument("EQ", D("NaN"), D("100"))
    with pytest.raises(AdvancedRiskError, match="greater than"):
        evaluate(shock="-1")
    with pytest.raises(AdvancedRiskError, match="duplicate"):
        evaluate(positions=(MarginPosition("EQ", D("-1")), MarginPosition("EQ", D("-2"))))
    with pytest.raises(AdvancedRiskError, match="explicitly price"):
        evaluate_margin_scenario(
            cash_after_financing_and_short_proceeds=D("10000"),
            positions=(MarginPosition("EQ", D("1")),),
            instruments=stock(),
            policy=policy(),
            shock_returns={},
        )


def test_margin_model_is_pure_and_deterministic():
    a = evaluate(shock="2")
    b = evaluate(shock="2")
    assert a == b
    assert not any(s in vars(__import__("lab.advanced_risk", fromlist=["*"])) for s in ("submit_order", "BrokerClient"))
