"""Synthetic source-pinned short funding bounds. No API or archive access."""

from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from lab import crash_funding
from lab.bearish_funding_audit import short_funding_bounds
from tests.test_bearish_equity import COSTS, candles, replay, trade

CUTOFF = pd.Timestamp("2026-01-03T00:00:00Z")


def evidence(*, rate_at_0800="-0.002"):
    stamps = pd.date_range("2026-01-01T00:00:00Z", periods=6, freq="8h")
    events = [
        {
            "fundingTime": int(t.value // 1_000_000),
            "fundingRate": rate_at_0800 if i == 1 else "0.001",
            "markPrice": "100",
        }
        for i, t in enumerate(stamps)
    ]
    plan = {
        "version": 2,
        "kind": crash_funding.KIND,
        "symbol": "BTCUSDT",
        "start": "2026-01-01",
        "holdout_start": "2026-01-03",
        "pages": [
            {
                "start_ms": int(stamps[0].normalize().value // 1_000_000),
                "end_ms": int(CUTOFF.value // 1_000_000) - 1,
                "rows": len(events),
                "source_sha256": "a" * 64,
            }
        ],
        "event_count": len(events),
        "events_sha256": crash_funding._digest(events),
    }
    return {"plan": plan, "events": events, "plan_sha256": crash_funding._digest(plan)}


def funded(r, source=None, *, boundary=CUTOFF, expected=None):
    source = source if source is not None else evidence()
    return short_funding_bounds(
        r,
        COSTS,
        source,
        boundary=boundary,
        expected_plan_sha256=expected or source["plan_sha256"],
    )


def make_trade(entry="2026-01-01T04:01:00Z", exit_="2026-01-01T12:00:00Z"):
    b = candles()
    return trade(b, pd.Timestamp(entry), pd.Timestamp(exit_))


def test_negative_certain_short_funding_replaces_not_adds_to_fixed_reserve():
    t = make_trade()
    result = funded(replay(t))
    assert len(result.trades) == 1
    row = result.trades[0]
    assert row.certain_settlements == 1
    assert row.ambiguous_settlements == 0
    assert row.funding_lower_return == pytest.approx(-0.002)
    assert row.net_lower_return == pytest.approx(t.net_return + t.funding_reserve_cost - 0.002)
    assert row.net_upper_return == pytest.approx(row.net_lower_return)
    assert result.lower_net_sum_not_portfolio == pytest.approx(row.net_lower_return)
    assert result.qualified is False


def test_positive_certain_short_funding_is_a_receipt():
    t = make_trade()
    r = funded(replay(t), evidence(rate_at_0800="0.003"))
    assert r.trades[0].funding_lower_return == pytest.approx(0.003)
    assert r.trades[0].funding_upper_return == pytest.approx(0.003)


def test_ambiguous_entry_payment_charges_lower_bound_only():
    t = make_trade("2026-01-01T08:01:00Z", "2026-01-01T08:05:00Z")
    r = funded(replay(t))
    row = r.trades[0]
    assert row.certain_settlements == 0
    assert row.ambiguous_settlements == 1
    assert row.funding_lower_return == pytest.approx(-0.002)
    assert row.funding_upper_return == pytest.approx(0)
    assert r.ambiguous_trades == 1


def test_ambiguous_exit_receipt_never_assumed_on_lower_bound():
    t = make_trade("2026-01-01T07:50:00Z", "2026-01-01T08:00:00Z")
    r = funded(replay(t), evidence(rate_at_0800="0.003"))
    row = r.trades[0]
    assert row.ambiguous_settlements == 1
    assert row.funding_lower_return == pytest.approx(0)
    assert row.funding_upper_return == pytest.approx(0.003)
    assert row.net_upper_return > row.net_lower_return


def test_complete_source_without_intrade_settlement_is_zero_funding():
    t = make_trade("2026-01-01T01:01:00Z", "2026-01-01T01:10:00Z")
    row = funded(replay(t)).trades[0]
    assert row.certain_settlements == row.ambiguous_settlements == 0
    assert row.net_lower_return == pytest.approx(t.net_return + t.funding_reserve_cost)


def test_source_hash_mismatch_or_mutated_event_refused():
    t = make_trade()
    source = evidence()
    previous_digest = source["plan_sha256"]
    altered = deepcopy(source)
    altered["events"][1]["fundingRate"] = "0.5"
    with pytest.raises(ValueError, match="count/hash"):
        funded(replay(t), altered)
    altered["plan"]["events_sha256"] = crash_funding._digest(altered["events"])
    altered["plan_sha256"] = crash_funding._digest(altered["plan"])
    with pytest.raises(ValueError, match="frozen research-plan digest"):
        funded(replay(t), altered, expected=previous_digest)


def test_missing_funding_event_or_gap_refused_without_assuming_zero():
    source = evidence()
    source["events"].pop(2)
    source["plan"]["event_count"] -= 1
    source["plan"]["pages"][0]["rows"] -= 1
    source["plan"]["events_sha256"] = crash_funding._digest(source["events"])
    source["plan_sha256"] = crash_funding._digest(source["plan"])
    with pytest.raises(ValueError, match="coverage gap"):
        funded(replay(make_trade()), source)


def test_out_of_order_or_noncanonical_source_refused():
    source = evidence()
    source["events"] = list(reversed(source["events"]))
    source["plan"]["events_sha256"] = crash_funding._digest(source["events"])
    source["plan_sha256"] = crash_funding._digest(source["plan"])
    with pytest.raises(ValueError, match="unordered"):
        funded(replay(make_trade()), source)

    source = evidence()
    source["events"][1]["fundingTime"] += 2_000_000
    source["plan"]["events_sha256"] = crash_funding._digest(source["events"])
    source["plan_sha256"] = crash_funding._digest(source["plan"])
    with pytest.raises(ValueError, match="funding hour"):
        funded(replay(make_trade()), source)


def test_halted_or_tampered_trade_refused():
    t = make_trade()
    with pytest.raises(ValueError, match="unresolved"):
        funded(replay(t, halted=True))
    with pytest.raises(ValueError, match="conservation"):
        funded(replay(replace(t, fee_cost=t.fee_cost + 0.001)))


def test_uncovered_trade_or_holdout_boundary_refused():
    t = make_trade()
    with pytest.raises(ValueError, match="UTC-midnight"):
        funded(replay(t), boundary=pd.Timestamp("2026-01-03T00:01:00Z"))
    with pytest.raises(ValueError, match="plan"):
        funded(replay(t), boundary=pd.Timestamp("2026-01-04T00:00:00Z"))
    late = make_trade("2026-01-02T23:51:00Z", "2026-01-03T00:01:00Z")
    with pytest.raises(ValueError, match="outside audited coverage"):
        funded(replay(late))


def test_overlapping_trades_rejected():
    t = make_trade()
    with pytest.raises(ValueError, match="overlapping"):
        funded(replay(t, t))


def test_funding_adapter_is_pure_and_preserves_trades_and_sources():
    source = evidence()
    t = make_trade()
    before = deepcopy(source)
    r = replay(t)
    funded(r, source)
    assert source == before
    assert r.trades == (t,)


def test_unbounded_round_trip_cost_budget_refused():
    from lab.bearish_ensemble import RoundTripCosts

    source = evidence()
    with pytest.raises(ValueError, match="round-trip cost budget"):
        short_funding_bounds(
            replay(),
            RoundTripCosts(5_000, 5, 2, 1),
            source,
            boundary=CUTOFF,
            expected_plan_sha256=source["plan_sha256"],
        )
