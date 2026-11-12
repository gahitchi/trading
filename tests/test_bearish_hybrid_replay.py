"""Synthetic-only causal OHLC replay tests. No archived or protected market data."""

from dataclasses import replace

import pytest

from lab.bearish_ensemble_replay import ReplayAssumptions
from lab.bearish_hybrid import MINUTE_NS, Bar, VirtualAccount
from lab.bearish_hybrid_replay import DAY_NS, HybridObservation, replay_hybrid_observations
from tests.test_bearish_hybrid import COSTS, PAPER, ZERO, crash_sequence, events, market_at, oi_at


def setup(*, exit_mode="target"):
    rows = crash_sequence()
    t = rows[-1].end_ns
    rows.append(Bar(t + MINUTE_NS, 97.8, 97.9, 97.7, 97.82, 100, 50))
    if exit_mode == "target":
        rows.append(Bar(t + 2 * MINUTE_NS, 97.82, 98.0, 96.7, 97.2, 500, 100))
    elif exit_mode == "both":
        rows.append(Bar(t + 2 * MINUTE_NS, 97.82, 98.5, 96.7, 97.8, 500, 100))
    elif exit_mode == "open_through_stop":
        rows.append(Bar(t + 2 * MINUTE_NS, 99.0, 99.1, 98.5, 98.6, 500, 100))
    elif exit_mode == "hold":
        rows.append(Bar(t + 2 * MINUTE_NS, 97.82, 98.0, 97.5, 97.8, 500, 100))
    else:
        raise ValueError(exit_mode)
    funding = events(rows[0].end_ns)
    records = [HybridObservation(bar, market_at(bar.end_ns), oi_at(bar.end_ns), funding, True, True) for bar in rows]
    boundary = (rows[-1].end_ns // DAY_NS + 1) * DAY_NS
    return records, boundary


def replay(records=None, boundary=None, *, account=PAPER, **kwargs):
    if records is None:
        records, boundary = setup()
    assert boundary is not None
    return replay_hybrid_observations(records, boundary_ns=boundary, costs=COSTS, account=account, **kwargs)


def test_delayed_short_target_has_real_costs_and_no_real_notional():
    rec, boundary = setup()
    result = replay(rec, boundary)
    assert len(result.funded_signal_times_ns) == len(result.trades) == len(result.virtual_notionals) == 1
    trade = result.trades[0]
    assert trade.entry_minute_start_ns == rec[-1].bar.end_ns - MINUTE_NS
    assert trade.signal_end_ns == rec[-3].bar.end_ns
    assert trade.exit_reason == "target"
    assert trade.entry_reference == pytest.approx(rec[-1].bar.open)
    assert trade.net_return < trade.gross_return
    assert trade.net_return == pytest.approx(
        trade.gross_return - trade.spread_cost - trade.slippage_cost - trade.fee_cost - trade.funding_reserve_cost
    )
    assert 0 < result.virtual_notionals[0] <= 0.30 * PAPER.equity
    assert result.halted is False and result.unresolved_after_entry == 0


def test_stop_has_priority_if_entry_candle_touches_both_barriers():
    rows, boundary = setup(exit_mode="both")
    result = replay(rows, boundary)
    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.net_return < 0
    assert trade.exit_reference >= trade.stop_reference


def test_zero_capital_only_emits_unfunded_signal_without_virtual_fills():
    rec, boundary = setup()
    result = replay(rec, boundary, account=ZERO)
    assert result.unfunded_signal_times_ns == (rec[-3].bar.end_ns,)
    assert not result.funded_signal_times_ns and not result.trades and not result.virtual_notionals


def test_signal_at_end_does_not_backfill_entry_or_assume_exits():
    rec, boundary = setup()
    result = replay(rec[:-2], boundary)
    assert len(result.funded_signal_times_ns) == 1
    assert not result.trades and result.skipped_before_entry == 1


def test_source_gap_after_open_fails_closed_instead_of_earning():
    rec, boundary = setup(exit_mode="hold")
    after = rec[-1].bar
    rec.append(replace(rec[-1], bar=Bar(after.end_ns + 2 * MINUTE_NS, 97.8, 98.0, 97.5, 97.7, 200, 70)))
    result = replay(rec, boundary)
    assert result.halted and result.unresolved_after_entry == 1
    assert not result.trades


def test_incomplete_entry_candle_cannot_be_reinterpreted_as_an_unfilled_order():
    rec, boundary = setup()
    rec[-1] = replace(rec[-1], source_complete=False)
    result = replay(rec, boundary)
    assert result.halted and result.unresolved_after_entry == 1 and not result.trades


def test_bad_pause_candle_resets_signal_warmup():
    rec, boundary = setup()
    rec[-4] = replace(rec[-4], source_complete=False)
    result = replay(rec, boundary)
    assert not result.funded_signal_times_ns and not result.trades


def test_gap_through_stop_at_entry_skips_trade_instead_of_adding_retroactive_loss():
    rec, boundary = setup(exit_mode="open_through_stop")
    result = replay(rec, boundary)
    assert result.skipped_before_entry == 1 and not result.trades and not result.halted


def test_unfinished_position_not_counted_as_completed_trade():
    rec, boundary = setup(exit_mode="hold")
    result = replay(rec, boundary)
    assert result.halted and result.unresolved_after_entry == 1 and not result.trades


def test_time_exit_includes_costs():
    rec, boundary = setup(exit_mode="hold")
    result = replay(rec, boundary, assumptions=ReplayAssumptions(1, 1))
    assert len(result.trades) == 1 and result.trades[0].exit_reason == "time"
    assert not result.halted


def test_protected_boundary_and_non_utc_midnight_rejected_before_any_outcome():
    rec, boundary = setup()
    with pytest.raises(ValueError, match="protected holdout"):
        replay(rec, (rec[0].bar.end_ns // DAY_NS) * DAY_NS)
    with pytest.raises(ValueError, match="midnight"):
        replay(rec, boundary + MINUTE_NS)
    with pytest.raises(ValueError, match="out-of-order"):
        replay([*rec, rec[-1]], boundary)


def test_no_immediate_entry_after_signal_even_if_old_assumptions_permit_it():
    rec, boundary = setup()
    with pytest.raises(ValueError, match="at least one fully closed minute"):
        replay(rec, boundary, assumptions=ReplayAssumptions(0, 15))


def test_daily_virtual_kill_mode_never_creates_exposure():
    rec, boundary = setup()
    for blocked in (replace(PAPER, kill_mode="halt"), replace(PAPER, day_pnl=-120), VirtualAccount()):
        result = replay(rec, boundary, account=blocked)
        assert not result.trades
