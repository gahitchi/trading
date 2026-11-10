"""Synthetic-only acceptance tests for conservative short OHLC replay."""

from dataclasses import replace

import pandas as pd
import pytest

from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_replay import ReplayAssumptions, replay_research_frame
from lab.bearish_ensemble_screen import screen_research_frame
from tests.test_bearish_ensemble_screen import boundary, history

COSTS = RoundTripCosts(5, 5, 2, 1)


def replay(b, *, costs=COSTS, assumptions=None):
    return replay_research_frame(b, boundary=boundary(b), costs=costs, assumptions=assumptions)


def test_bearish_replay_uses_delayed_short_only_entry_and_reconciles_costs():
    b = history(240)
    r = replay(b)
    assert len(r.census.candidate_times_ns) == 1
    assert len(r.trades) == 1
    assert not r.halted and not r.unresolved_after_entry
    trade = r.trades[0]
    assert trade.signal_end_ns == int(b.index[200].value)
    assert trade.entry_minute_start_ns == int(b.index[201].value)
    assert trade.exit_bar_end_ns > trade.entry_minute_start_ns
    assert trade.entry_fill < trade.entry_reference
    assert trade.exit_fill > trade.exit_reference
    assert trade.net_return == pytest.approx(
        trade.gross_return - trade.fee_cost - trade.slippage_cost - trade.spread_cost - trade.funding_reserve_cost
    )
    assert r.total_net_trade_return == trade.net_return
    assert trade.exit_reason == "target"


def test_stop_over_target_on_ambiguous_candle():
    b = history(220)
    census = screen_research_frame(b, boundary=boundary(b), costs=COSTS)
    assert len(census.candidate_times_ns) == 1
    entry = float(b.iloc[202]["open"])
    stop = entry * (1 + census.candidate_stops_bps[0] / 10_000)
    target = entry * (1 - census.candidate_targets_bps[0] / 10_000)
    b.loc[b.index[202], "high"] = stop + 0.5
    b.loc[b.index[202], "low"] = target - 0.5
    t = replay(b).trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_reference == pytest.approx(stop)
    assert t.net_return < 0


def test_gapped_stop_uses_worse_open_not_favorable_stop():
    b = history(220)
    census = screen_research_frame(b, boundary=boundary(b), costs=COSTS)
    entry = float(b.iloc[202]["open"])
    stop = entry * (1 + census.candidate_stops_bps[0] / 10_000)
    # Bar 202 opens normally; keep its range inside stop and target.
    b.loc[b.index[202], "high"] = entry + 0.01
    b.loc[b.index[202], "low"] = entry - 0.01
    b.loc[b.index[202], "close"] = entry
    # An adverse gap between bars crosses the protective stop.
    b.loc[b.index[203], ["open", "high", "low", "close"]] = [stop + 0.2, stop + 0.3, stop + 0.1, stop + 0.2]
    t = replay(b).trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_reference == pytest.approx(stop + 0.2)


def test_time_exit_after_exact_number_of_complete_bars():
    b = history(210)
    r = replay(b, assumptions=ReplayAssumptions(max_hold_minutes=2))
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.exit_reason == "time"
    assert t.exit_bar_end_ns == int(b.index[203].value)


def test_unknown_inventory_path_halts_without_credited_trade():
    b = history(240)
    b.loc[b.index[203], "oi_invalid"] = True
    r = replay(b)
    assert r.census.candidate_times_ns
    assert not r.trades
    assert r.halted and r.unresolved_after_entry == 1


def test_entry_candle_quality_is_not_used_to_avoid_an_entry():
    b = history(240)
    # The flag on candle 202 is unknown at candle 202's OPEN.
    # The modeled short cannot be retroactively canceled.
    b.loc[b.index[202], "oi_invalid"] = True
    r = replay(b)
    assert not r.trades
    assert r.halted and r.unresolved_after_entry == 1
    assert r.skipped_before_entry == 0


def test_quality_known_before_entry_can_veto_the_trade():
    b = history(240)
    # Candle 201 completes exactly as the modeled entry starts.
    b.loc[b.index[201], "oi_invalid"] = True
    r = replay(b)
    assert not r.trades and not r.halted
    assert r.skipped_before_entry == 1


def test_insufficient_tail_cannot_be_counted_as_a_completed_trade():
    b = history(201)
    r = replay(b)
    assert r.census.candidate_times_ns
    assert r.skipped_before_entry == 1
    assert not r.trades and not r.halted


def test_negative_spread_or_future_holdout_is_rejected():
    b = history(240)
    with pytest.raises(ValueError, match="cost"):
        replay(b, costs=RoundTripCosts(5, 5, -2))
    with pytest.raises(ValueError, match="protected holdout"):
        replay_research_frame(b, boundary=b.index[-1].normalize(), costs=COSTS)
    with pytest.raises(ValueError):
        ReplayAssumptions(max_hold_minutes=0)


def test_replay_does_not_modify_source_frame():
    b = history(240)
    original = b.copy(deep=True)
    replay(b)
    pd.testing.assert_frame_equal(b, original)


def test_higher_costs_do_not_improve_modeled_short_trade():
    b = history(240)
    base = replay(b)
    higher = replay(b, costs=replace(COSTS, slippage_per_side_bps=8))
    assert len(base.trades) == len(higher.trades) == 1
    assert higher.trades[0].net_return < base.trades[0].net_return
