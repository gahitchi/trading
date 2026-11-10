"""Synthetic-only chronological funding-to-daily-equity acceptance tests."""

from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from lab import crash_funding
from lab.bearish_equity import EquityAssumptions
from lab.bearish_funded_equity import funded_daily_short_equity
from tests.test_bearish_equity import COSTS, candles, replay, trade
from tests.test_bearish_funding_audit import evidence as previous_evidence

CUTOFF = pd.Timestamp("2026-01-04T00:00:00Z")


def evidence(*, rate_at_0800="-0.002"):
    """Complete pinned synthetic funding coverage extending past both UTC days.

    The second day's 00:00 candle END must remain strictly BEFORE
    the funding plan's protected cutoff, not on that cutoff.
    """
    source = previous_evidence(rate_at_0800=rate_at_0800)
    for stamp in pd.date_range("2026-01-03T00:00:00Z", periods=3, freq="8h"):
        source["events"].append(
            {"fundingTime": int(stamp.value // 1_000_000), "fundingRate": "0.001", "markPrice": "100"}
        )
    plan = source["plan"]
    plan["holdout_start"] = CUTOFF.date().isoformat()
    plan["pages"][0]["end_ms"] = int(CUTOFF.value // 1_000_000) - 1
    plan["pages"][0]["rows"] = len(source["events"])
    plan["event_count"] = len(source["events"])
    plan["events_sha256"] = crash_funding._digest(source["events"])
    source["plan_sha256"] = crash_funding._digest(plan)
    return source


def short(b, entry, exit_):
    entry, exit_ = pd.Timestamp(entry), pd.Timestamp(exit_)
    b.loc[exit_, "close"] = 99.0
    return trade(b, entry, exit_)


def account(b, r, source=None, *, policy=None, boundary=CUTOFF, expected=None):
    source = evidence() if source is None else source
    return funded_daily_short_equity(
        b,
        r,
        COSTS,
        source,
        boundary=boundary,
        expected_plan_sha256=expected or source["plan_sha256"],
        assumptions=policy,
    )


def test_negative_settlement_is_realized_as_cost_not_added_to_reserve():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    r = account(b, replay(t))
    expected_return = 0.25 * (t.net_return + t.funding_reserve_cost - 0.002)
    assert r.funded_settlements == 1
    assert r.end_equity == pytest.approx(10_000 * (1 + expected_return))
    assert r.days[0].funding_cashflow == pytest.approx(-5.0)
    assert r.days[1].net_pnl == 0
    assert r.days[0].funding_settlements == 1
    assert r.qualified is False


def test_positive_funding_is_paid_to_short_at_settlement():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    r = account(b, replay(t), evidence(rate_at_0800="0.003"))
    assert r.days[0].funding_cashflow == pytest.approx(7.5)
    assert r.end_equity == pytest.approx(10_000 * (1 + 0.25 * (t.net_return + t.funding_reserve_cost + 0.003)))


def test_source_complete_zero_settlement_does_not_charge_frozen_reserve():
    b = candles()
    t = short(b, "2026-01-01T01:01:00Z", "2026-01-01T01:10:00Z")
    result = account(b, replay(t))
    assert result.funded_settlements == 0
    assert result.end_equity == pytest.approx(10_000 * (1 + 0.25 * (t.net_return + t.funding_reserve_cost)))
    assert all(d.funding_cashflow == 0 for d in result.days)


def test_open_overnight_short_posts_midnight_settlement_to_first_utc_day():
    b = candles()
    t = short(b, "2026-01-01T22:01:00Z", "2026-01-02T10:00:00Z")
    result = account(b, replay(t))
    first, second = result.days
    assert first.open_position and not second.open_position
    assert first.entries == 1 and first.exits == 0
    assert second.entries == 0 and second.exits == 1
    assert first.funding_settlements == 1
    assert second.funding_settlements == 1
    assert first.funding_cashflow == pytest.approx(2.5)
    assert second.funding_cashflow == pytest.approx(2.5)
    assert first.unrealized_after_closeout < 0
    assert result.end_equity == pytest.approx(10_000 * (1 + 0.25 * (t.net_return + t.funding_reserve_cost + 0.002)))
    assert sum(row.net_pnl for row in result.days) == pytest.approx(result.end_equity - 10_000)


def test_sequential_shorts_reinvest_net_settlement_in_realized_cash():
    b = candles()
    first = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    second = short(b, "2026-01-02T04:01:00Z", "2026-01-02T12:00:00Z")
    r = account(b, replay(first, second))
    first_return = first.net_return + first.funding_reserve_cost - 0.002
    second_return = second.net_return + second.funding_reserve_cost + 0.001
    expected = 10_000 * (1 + 0.25 * first_return) * (1 + 0.25 * second_return)
    assert r.end_equity == pytest.approx(expected)
    assert [row.entries for row in r.days] == [1, 1]
    assert [row.exits for row in r.days] == [1, 1]
    assert r.funded_settlements == 2


def test_complete_source_and_flat_portfolio_reports_both_zero_trade_days():
    r = account(candles(), replay())
    assert r.completed_trades == 0
    assert r.end_equity == 10_000
    assert r.flat_days == 2
    assert r.funded_settlements == 0
    assert all(d.daily_return == 0 for d in r.days)


def test_ambiguous_entry_or_exit_funding_refused_for_settled_cash():
    b = candles()
    t = short(b, "2026-01-01T08:01:00Z", "2026-01-01T08:05:00Z")
    with pytest.raises(ValueError, match="ambiguous funding ownership"):
        account(b, replay(t))
    b = candles()
    t = short(b, "2026-01-01T07:50:00Z", "2026-01-01T08:00:00Z")
    with pytest.raises(ValueError, match="ambiguous funding ownership"):
        account(b, replay(t))


def test_source_mutation_or_wrong_registration_digest_fails_closed():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    src = evidence()
    mutated = deepcopy(src)
    mutated["events"][1]["fundingRate"] = "0.8"
    with pytest.raises(ValueError, match="count/hash"):
        account(b, replay(t), mutated)
    with pytest.raises(ValueError, match="frozen research-plan digest"):
        account(b, replay(t), src, expected="0" * 64)


def test_holdout_gaps_and_halted_inventory_still_refused():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    with pytest.raises(ValueError, match="protected holdout"):
        account(b, replay(t), boundary=pd.Timestamp("2026-01-02T00:00:00Z"))
    with pytest.raises(ValueError, match="gaps"):
        account(b.drop(index=b.index[50]), replay(t))
    with pytest.raises(ValueError, match="unresolved"):
        account(b, replay(t, halted=True))


def test_funding_debit_can_breach_conservative_intrabar_equity_floor():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    policy = EquityAssumptions(notional_fraction=1.0)
    with pytest.raises(ValueError, match="funded intrabar equity floor"):
        account(b, replay(t), evidence(rate_at_0800="-0.95"), policy=policy)


def test_tampered_trade_rejected_and_source_and_replay_unchanged():
    b = candles()
    t = short(b, "2026-01-01T04:01:00Z", "2026-01-01T12:00:00Z")
    source = evidence()
    before = deepcopy(source)
    result = account(b, replay(t), source)
    assert source == before and result.completed_trades == 1
    with pytest.raises(ValueError, match="conservation"):
        account(b, replay(replace(t, fee_cost=t.fee_cost + 0.0005)), source)


def test_exact_protected_cutoff_candle_is_not_assumed_research_data():
    # This is the original fixture bug: Jan 3 00:00 is the last
    # completed price candle but must not equal the protected boundary.
    b = candles()
    with pytest.raises(ValueError, match="protected holdout"):
        account(b, replay(), boundary=pd.Timestamp("2026-01-03T00:00:00Z"))
