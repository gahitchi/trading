"""Synthetic-only, no-network interval accounting for ambiguous short funding."""

from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from lab.bearish_funded_equity import funded_daily_short_equity
from lab.bearish_funding_interval_equity import interval_daily_short_equity
from tests.test_bearish_equity import COSTS, candles, replay
from tests.test_bearish_funded_equity import CUTOFF, evidence, short


def interval(b, r, source=None, *, boundary=CUTOFF, expected=None):
    source = evidence() if source is None else source
    return interval_daily_short_equity(
        b,
        r,
        COSTS,
        source,
        boundary=boundary,
        expected_plan_sha256=expected or source["plan_sha256"],
    )


def no_reserve(t):
    return t.net_return + t.funding_reserve_cost


def test_flat_account_retains_both_zero_trade_days():
    result = interval(candles(), replay())
    assert result.completed_trades == result.certain_settlements == result.ambiguous_settlements == 0
    assert len(result.days) == 2
    assert all(d.lower_end_equity == d.upper_end_equity == 10_000 for d in result.days)
    assert result.lower_total_return == result.upper_total_return == 0
    assert not result.qualified


def test_certain_negative_funding_agrees_with_strict_cash_ledger():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    source = evidence()
    interval_result = interval(b, replay(t), source)
    strict = funded_daily_short_equity(
        b, replay(t), COSTS, source, boundary=CUTOFF, expected_plan_sha256=source["plan_sha256"]
    )
    assert interval_result.certain_settlements == 1
    assert interval_result.ambiguous_settlements == 0
    assert interval_result.lower_end_equity == pytest.approx(strict.end_equity)
    assert interval_result.upper_end_equity == pytest.approx(strict.end_equity)
    assert interval_result.days[0].lower_path_funding_cashflow == pytest.approx(-5.0)


def test_ambiguous_negative_funding_keeps_trade_and_debit_lower_only():
    b = candles()
    t = short(b, "2026-01-01T08:01:00Z", "2026-01-01T08:05:00Z")
    result = interval(b, replay(t))
    assert result.completed_trades == 1
    assert result.ambiguous_settlements == 1
    assert result.days[0].lower_path_funding_cashflow == pytest.approx(-5.0)
    assert result.days[0].upper_path_funding_cashflow == 0
    assert result.lower_end_equity == pytest.approx(10_000 * (1 + 0.25 * (no_reserve(t) - 0.002)))
    assert result.upper_end_equity == pytest.approx(10_000 * (1 + 0.25 * no_reserve(t)))


def test_ambiguous_positive_funding_is_upper_only_even_at_exit_boundary():
    b = candles()
    t = short(b, "2026-01-01T07:50:00Z", "2026-01-01T08:00:00Z")
    result = interval(b, replay(t), evidence(rate_at_0800="0.003"))
    assert result.ambiguous_settlements == 1
    assert result.days[0].lower_path_funding_cashflow == 0
    assert result.days[0].upper_path_funding_cashflow == pytest.approx(7.5)
    assert result.upper_end_equity == pytest.approx(10_000 * (1 + 0.25 * (no_reserve(t) + 0.003)))


def test_event_after_exit_uncertainty_window_cannot_be_booked():
    b = candles()
    t = short(b, "2026-01-01T07:50:00Z", "2026-01-01T07:59:00Z")
    result = interval(b, replay(t), evidence(rate_at_0800="0.003"))
    assert result.ambiguous_settlements == 0
    assert result.days[0].upper_path_funding_cashflow == 0
    assert result.lower_end_equity == pytest.approx(result.upper_end_equity)


def test_overnight_funding_posted_by_utc_day_not_final_exit_day():
    b = candles()
    t = short(b, "2026-01-01T22:01:00Z", "2026-01-02T10:00:00Z")
    result = interval(b, replay(t))
    first, second = result.days
    assert first.open_position and not second.open_position
    assert first.lower_path_funding_cashflow == pytest.approx(2.5)
    assert second.lower_path_funding_cashflow == pytest.approx(2.5)
    assert result.lower_end_equity == pytest.approx(10_000 * (1 + 0.25 * (no_reserve(t) + 0.002)))


def test_uncertain_first_trade_changes_later_realized_reinvestment():
    b = candles()
    first = short(b, "2026-01-01T08:01:00Z", "2026-01-01T08:05:00Z")
    second = short(b, "2026-01-02T04:01:00Z", "2026-01-02T12:00:00Z")
    result = interval(b, replay(first, second))
    low_one = 1 + 0.25 * (no_reserve(first) - 0.002)
    upper_one = 1 + 0.25 * no_reserve(first)
    second_factor = 1 + 0.25 * (no_reserve(second) + 0.001)
    assert result.completed_trades == 2
    assert result.ambiguous_settlements == 1
    assert result.certain_settlements == 1
    assert result.lower_end_equity == pytest.approx(10_000 * low_one * second_factor)
    assert result.upper_end_equity == pytest.approx(10_000 * upper_one * second_factor)
    assert result.days[1].lower_path_funding_cashflow < result.days[1].upper_path_funding_cashflow


def test_pinned_source_mutation_and_wrong_hash_rejected():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    source = evidence()
    before = deepcopy(source)
    interval(b, replay(t), source)
    assert source == before
    bad = deepcopy(source)
    bad["events"][1]["fundingRate"] = "0.4"
    with pytest.raises(ValueError, match="count/hash"):
        interval(b, replay(t), bad)
    with pytest.raises(ValueError, match="frozen research-plan digest"):
        interval(b, replay(t), source, expected="0" * 64)


def test_mandatory_cutoff_and_missing_bars_fail_closed():
    b = candles()
    with pytest.raises(ValueError, match="protected holdout"):
        interval(b, replay(), boundary=pd.Timestamp("2026-01-03T00:00:00Z"))
    with pytest.raises(ValueError, match="gaps"):
        interval(b.drop(b.index[10]), replay())


def test_unresolved_or_modified_trade_fails_before_accounting():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    with pytest.raises(ValueError, match="unresolved"):
        interval(b, replay(t, halted=True))
    with pytest.raises(ValueError, match="conservation"):
        interval(b, replay(replace(t, net_return=t.net_return + 0.1)))


def test_complete_source_but_no_settlement_within_trade_means_no_reserve():
    b = candles()
    t = short(b, "2026-01-01T01:01:00Z", "2026-01-01T01:10:00Z")
    r = interval(b, replay(t))
    assert r.certain_settlements == r.ambiguous_settlements == 0
    assert r.lower_end_equity == pytest.approx(10_000 * (1 + 0.25 * no_reserve(t)))
    assert r.upper_end_equity == pytest.approx(r.lower_end_equity)


def test_midnight_entry_and_coincident_funding_belong_to_next_utc_day():
    b = candles()
    t = short(b, "2026-01-02T00:01:00Z", "2026-01-02T00:05:00Z")
    result = interval(b, replay(t))
    first, second = result.days
    assert first.lower_end_equity == first.upper_end_equity == 10_000
    assert not first.open_position
    assert first.lower_path_funding_cashflow == first.upper_path_funding_cashflow == 0
    assert result.ambiguous_settlements == 1
    assert second.lower_path_funding_cashflow == 0
    assert second.upper_path_funding_cashflow == pytest.approx(2.5)


def test_midnight_exit_and_ambiguous_funding_post_to_preceding_utc_day():
    b = candles()
    t = short(b, "2026-01-01T22:01:00Z", "2026-01-02T00:00:00Z")
    result = interval(b, replay(t))
    first, second = result.days
    assert result.ambiguous_settlements == 1
    assert not first.open_position
    assert first.lower_path_funding_cashflow == 0
    assert first.upper_path_funding_cashflow == pytest.approx(2.5)
    assert second.lower_path_funding_cashflow == second.upper_path_funding_cashflow == 0
    assert first.lower_end_equity < first.upper_end_equity
