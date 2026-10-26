"""Gold signal, lot exception, and protective-level regression checks."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from lab.gold import GoldBar, plan_gold_trade


def crossing_bars():
    start = date(2025, 1, 1)
    closes = [100] * 150 + [110] * 49 + [90, 115]
    return [
        GoldBar(start + timedelta(days=i), Decimal(c), Decimal(c + 1), Decimal(c - 1), Decimal(c))
        for i, c in enumerate(closes)
    ]


def test_gold_uses_one_ounce_when_one_percent_cannot_buy_minimum():
    plan = plan_gold_trade(crossing_bars(), equity_usd=Decimal("1000"), price_tick_usd=Decimal("0.01"))
    assert plan is not None
    assert plan.quantity_oz == 1
    assert plan.minimum_lot_override
    assert plan.notional_usd > plan.allocated_usd
    assert plan.target_usd - plan.reference_usd >= 2 * (plan.reference_usd - plan.stop_usd)


def test_gold_sizing_respects_one_percent_when_budget_suffices():
    plan = plan_gold_trade(crossing_bars(), equity_usd=Decimal("1000000"), price_tick_usd=Decimal("0.01"))
    assert plan is not None
    assert plan.quantity_oz == 86
    assert not plan.minimum_lot_override
    assert plan.notional_usd <= plan.allocated_usd


def test_gold_requires_closed_history_and_an_actual_crossing():
    bars = crossing_bars()
    with pytest.raises(ValueError, match="201 closed"):
        plan_gold_trade(bars[:-1], equity_usd=Decimal("1000000"), price_tick_usd=Decimal("0.01"))
    bars[-1] = GoldBar(bars[-1].session, Decimal(90), Decimal(91), Decimal(89), Decimal(90))
    assert plan_gold_trade(bars, equity_usd=Decimal("1000000"), price_tick_usd=Decimal("0.01")) is None
