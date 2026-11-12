"""Research-only, synthetic minute-series tests. Protected market data never loaded."""

from dataclasses import replace

import pytest

from lab.bearish_ensemble_replay import ReplayAssumptions
from lab.bearish_hybrid import MINUTE_NS, Bar, Risk, VirtualAccount
from lab.bearish_hybrid_dynamic import ProfitPolicy, replay_hybrid_dynamic
from lab.bearish_hybrid_replay import DAY_NS, HybridObservation
from tests.test_bearish_hybrid import COSTS, PAPER, events, market_at, oi_at

MIDNIGHT = 1_700_006_400_000_000_000
assert MIDNIGHT % DAY_NS == 0


def series(*, days=2, events_at=((120, "win"), (320, "win"))):
    """Each synthetic fall contains a separate pause and downside confirmation."""
    end_ns = MIDNIGHT + days * DAY_NS
    bars = [Bar(MIDNIGHT + (i + 1) * MINUTE_NS, 100, 100.1, 99.9, 100, 100, 50) for i in range(days * 1440)]
    for at, outcome in events_at:
        assert at >= 90 and at + 4 < len(bars)
        t = bars[at].end_ns
        bars[at] = Bar(t, 100, 100.1, 97.9, 98, 3_000, 100)
        bars[at + 1] = Bar(t + MINUTE_NS, 98, 98.1, 97.98, 98.05, 100, 50)
        bars[at + 2] = Bar(t + 2 * MINUTE_NS, 98.05, 98.09, 97.79, 97.8, 200, 30)
        bars[at + 3] = Bar(t + 3 * MINUTE_NS, 97.8, 97.9, 97.7, 97.82, 100, 50)
        if outcome == "win":
            bars[at + 4] = Bar(t + 4 * MINUTE_NS, 97.82, 98.0, 96.7, 97.2, 500, 100)
        elif outcome == "loss":
            bars[at + 4] = Bar(t + 4 * MINUTE_NS, 97.82, 98.4, 97.7, 98.2, 500, 100)
        elif outcome == "hold":
            bars[at + 4] = Bar(t + 4 * MINUTE_NS, 97.82, 98.0, 97.5, 97.8, 500, 100)
        else:
            raise ValueError(outcome)
    known = events(bars[0].end_ns)
    observations = [HybridObservation(b, market_at(b.end_ns), oi_at(b.end_ns), known, True, True) for b in bars]
    return observations, end_ns + DAY_NS


def run(observations=None, boundary=None, *, fraction=1.0, account=PAPER, **kwargs):
    if observations is None:
        observations, boundary = series()
    return replay_hybrid_dynamic(
        observations,
        boundary_ns=boundary,
        costs=COSTS,
        account=account,
        profit_policy=ProfitPolicy(fraction),
        **kwargs,
    )


def test_reinvesting_gains_resizes_later_trade_without_retroactive_signal_change():
    all_in = run(fraction=1.0)
    reserved = run(fraction=0.0)
    assert len(all_in.trades) == len(reserved.trades) == 2
    assert all(t.exit_reason == "target" for t in all_in.trades)
    assert all_in.virtual_notionals[0] == pytest.approx(reserved.virtual_notionals[0])
    assert all_in.virtual_notionals[1] > reserved.virtual_notionals[1]
    assert all_in.trading_equity_at_entries[1] > all_in.trading_equity_at_entries[0]
    assert reserved.trading_equity_at_entries[1] == pytest.approx(reserved.trading_equity_at_entries[0])
    assert all_in.reserved_virtual_profits == 0
    assert reserved.reserved_virtual_profits > 0
    assert all_in.ending_total_virtual_equity > reserved.ending_total_virtual_equity
    assert all_in.actual_notional == 0 and not all_in.qualified


def test_loss_debits_trading_cash_even_with_zero_reinvestment():
    rec, bound = series(events_at=((120, "loss"), (320, "loss")))
    result = run(rec, bound, fraction=0.0)
    assert len(result.trades) == 2 and all(t.net_return < 0 for t in result.trades)
    assert result.virtual_notionals[1] < result.virtual_notionals[0]
    assert result.trading_equity_at_entries[1] < result.trading_equity_at_entries[0]
    assert result.reserved_virtual_profits == 0
    assert result.ending_total_virtual_equity < PAPER.equity
    assert result.days[0].net_pnl < 0


def test_split_profits_sum_to_total_equity_without_double_counting():
    report = run(fraction=0.25)
    gains = [
        notional * trade.net_return for trade, notional in zip(report.trades, report.virtual_notionals, strict=True)
    ]
    assert all(p > 0 for p in gains)
    assert report.reserved_virtual_profits == pytest.approx(sum(gains) * 0.75)
    assert report.ending_trading_cash == pytest.approx(PAPER.equity + sum(gains) * 0.25)
    assert report.ending_total_virtual_equity == pytest.approx(PAPER.equity + sum(gains))
    assert sum(day.net_pnl for day in report.days) == pytest.approx(sum(gains))


def test_flat_days_are_explicit_and_do_not_produce_false_income():
    rec, bound = series(days=3, events_at=())
    report = run(rec, bound)
    assert len(report.days) == 3 and report.flat_days == 3
    assert report.completed_trades == report.positive_days == report.negative_days == 0
    assert report.ending_total_virtual_equity == PAPER.equity


def test_daily_reset_after_midnight_with_no_profit_transfer_as_loss():
    rec, bound = series(events_at=((120, "win"), (1550, "win")))
    report = run(rec, bound, fraction=0.0)
    assert len(report.trades) == 2
    assert all(day.net_pnl > 0 for day in report.days)
    assert report.days[1].start_equity == pytest.approx(report.days[0].end_equity)
    assert report.reserved_virtual_profits > 0


def test_replay_does_not_consult_future_candle_at_signal_bar():
    rec, bound = series(events_at=((120, "win"),))
    base = run(rec, bound)
    changed = list(rec)
    trade_i = 124
    # Change the would-be entry's future price to an impossible long-side price.
    b = changed[trade_i].bar
    changed[trade_i] = replace(changed[trade_i], bar=replace(b, open=99, high=99.3, low=98.8, close=99))
    later = run(changed, bound)
    assert base.candidate_signals == later.candidate_signals == 1
    assert later.skipped_entries == 1 and not later.trades


def test_conservative_stop_first_if_both_barriers_touched():
    rec, bound = series(events_at=((120, "loss"),))
    both = list(rec)
    candle = both[124].bar
    both[124] = replace(both[124], bar=replace(candle, low=96.0))
    report = run(both, bound)
    assert report.trades[0].exit_reason == "stop"
    assert report.trades[0].net_return < 0


def test_missing_candle_and_protected_boundary_reject_all_accounting():
    rec, bound = series(events_at=((120, "win"),))
    bad = list(rec)
    bad[200] = replace(bad[200], bar=replace(bad[200].bar, end_ns=bad[200].bar.end_ns + MINUTE_NS))
    with pytest.raises(ValueError, match="continuous minute"):
        run(bad, bound)
    with pytest.raises(ValueError, match="untrusted"):
        run([*rec[:130], replace(rec[130], source_complete=False), *rec[131:]], bound)
    with pytest.raises(ValueError, match="protected holdout"):
        run(rec, rec[-1].bar.end_ns)


def test_partial_utc_day_refused_and_zero_virtual_cash_uses_signal_only_replay():
    rec, bound = series()
    with pytest.raises(ValueError, match="complete UTC day"):
        run(rec[1:], bound)
    with pytest.raises(ValueError, match="funded virtual cash"):
        run(rec, bound, account=VirtualAccount())


@pytest.mark.parametrize("value", [-1, 1.1, float("nan"), float("inf"), True, None])
def test_profit_policy_rejects_invalid_fraction(value):
    with pytest.raises(ValueError, match="reinvestment"):
        ProfitPolicy(value)


def test_kill_mode_blocks_all_virtual_exposure():
    rec, bound = series()
    blocked = replace(PAPER, kill_mode="halt")
    result = run(rec, bound, account=blocked)
    assert result.trades == () and result.ending_total_virtual_equity == PAPER.equity


def test_delayed_entry_and_max_holding_window_preserved():
    rec, bound = series(events_at=((120, "hold"),))
    report = run(rec, bound, assumptions=ReplayAssumptions(1, 1))
    trade = report.trades[0]
    assert trade.exit_reason == "time"
    assert trade.entry_minute_start_ns == rec[124].bar.end_ns - MINUTE_NS
    assert trade.exit_bar_end_ns == rec[124].bar.end_ns


def test_virtual_position_cannot_be_created_by_unverified_funding():
    rec, bound = series()
    edited = [replace(r, funding_verified=False) for r in rec]
    report = run(edited, bound)
    assert not report.trades and not report.candidate_signals


def test_more_conservative_risk_cap_reduces_virtual_trade_notional():
    original = run()
    small = run(risk=Risk(max_symbol_notional_fraction=0.10))
    assert len(small.trades) == len(original.trades)
    assert small.virtual_notionals[0] <= 1000.0
    assert small.virtual_notionals[0] < original.virtual_notionals[0]


def test_overnight_open_short_marks_daily_equity_and_realizes_once():
    rec, bound = series(events_at=((1435, "hold"),))
    next_day = list(rec)
    b0 = next_day[1440].bar
    next_day[1440] = replace(next_day[1440], bar=replace(b0, open=97.8, high=98.0, low=97.5, close=97.75))
    b1 = next_day[1441].bar
    next_day[1441] = replace(next_day[1441], bar=replace(b1, open=97.75, high=97.9, low=96.7, close=97.2))
    report = run(next_day, bound, fraction=0.5)
    assert len(report.trades) == 1
    assert report.trades[0].exit_reason == "target"
    first, second = report.days
    assert first.open_position and first.entries == 1 and first.exits == 0
    assert first.realized_cash == PAPER.equity
    assert first.unrealized_after_closeout < 0
    assert not second.open_position and second.entries == 0 and second.exits == 1
    assert sum(d.net_pnl for d in report.days) == pytest.approx(report.ending_total_virtual_equity - PAPER.equity)


def test_reserved_virtual_profits_are_not_spent_on_subsequent_loss():
    rec, bound = series(events_at=((120, "win"), (320, "loss")))
    report = run(rec, bound, fraction=0)
    profit = report.virtual_notionals[0] * report.trades[0].net_return
    loss = report.virtual_notionals[1] * report.trades[1].net_return
    assert profit > 0 and loss < 0
    assert report.reserved_virtual_profits == pytest.approx(profit)
    assert report.ending_trading_cash == pytest.approx(PAPER.equity + loss)
    assert report.ending_total_virtual_equity == pytest.approx(PAPER.equity + profit + loss)


def test_intrabar_insolvency_is_not_fabricated_as_a_filled_trade():
    rec, bound = series(events_at=((120, "win"),))
    edited = list(rec)
    b = edited[124].bar
    edited[124] = replace(edited[124], bar=replace(b, high=100_000))
    with pytest.raises(ValueError, match="insolvency"):
        run(edited, bound)


def test_no_collateral_no_virtual_positions_even_with_positive_equity():
    rec, bound = series(events_at=((120, "win"),))
    report = run(rec, bound, account=replace(PAPER, free_collateral=0))
    assert not report.trades and report.ending_total_virtual_equity == PAPER.equity
