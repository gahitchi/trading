"""The London Gold preview stays read-only and uses only complete, received bars."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lab.gold import GoldBar, gold_atr_trend_preview


def _bars():
    start = datetime(2026, 1, 1, 22, tzinfo=UTC)
    closes = [Decimal(3400 + 3 * i) for i in range(198)] + [Decimal(3850), Decimal(4000)]
    return [
        GoldBar(
            start + timedelta(days=i),
            start + timedelta(days=i, minutes=1),
            close,
            close + 10,
            close - 10,
            close,
        )
        for i, close in enumerate(closes)
    ]


def _preview(bars, *, capital_usd=Decimal(1_000_000), as_of=None, price_tick_usd=Decimal("0.01")):
    return gold_atr_trend_preview(
        bars,
        as_of=as_of or bars[-1].available_at,
        capital_usd=capital_usd,
        price_tick_usd=price_tick_usd,
    )


@pytest.mark.parametrize("tick", ["0.01", "0.05"])
def test_gold_trend_allocates_one_percent_and_uses_two_to_one_atr_levels(tick):
    bars = _bars()
    plan = _preview(bars, price_tick_usd=Decimal(tick))
    assert plan is not None
    assert plan.ounces == 2
    assert plan.notional_usd == Decimal(8000)
    assert plan.allocated_usd == Decimal(10000)
    assert plan.stop_reference_usd < plan.entry_reference_usd < plan.target_reference_usd
    assert plan.target_reference_usd - plan.entry_reference_usd >= 2 * (
        plan.entry_reference_usd - plan.stop_reference_usd
    )
    assert plan.entry_reference_usd - plan.stop_reference_usd >= 2 * plan.atr_usd
    assert plan.stop_reference_usd % plan.price_tick_usd == 0
    assert plan.target_reference_usd % plan.price_tick_usd == 0


def test_gold_preview_abstains_without_signal_or_minimum_one_ounce():
    bars = _bars()
    assert _preview(bars, capital_usd=Decimal(300_000)) is None
    bars[-1] = GoldBar(
        bars[-1].end,
        bars[-1].available_at,
        Decimal(3800),
        Decimal(3810),
        Decimal(3790),
        Decimal(3800),
    )
    assert _preview(bars) is None


def test_gold_preview_rejects_a_bar_not_yet_available():
    bars = _bars()
    with pytest.raises(ValueError, match="available"):
        _preview(bars, as_of=bars[-1].end)


def test_gold_preview_rejects_stale_data():
    bars = _bars()
    with pytest.raises(ValueError, match="stale"):
        _preview(bars, as_of=bars[-1].available_at + timedelta(hours=25))


def test_gold_preview_rejects_mixed_or_non_daily_bars():
    bars = _bars()
    bars[-1] = GoldBar(
        bars[-2].end + timedelta(hours=1),
        bars[-2].available_at + timedelta(hours=1),
        bars[-1].open,
        bars[-1].high,
        bars[-1].low,
        bars[-1].close,
    )
    with pytest.raises(ValueError, match="ordered"):
        _preview(bars)


def test_gold_preview_requires_verified_tick():
    bars = _bars()
    with pytest.raises(ValueError, match="price_tick_usd"):
        _preview(bars, price_tick_usd=Decimal(0))
