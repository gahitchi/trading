"""No-network, synthetic-only acceptance tests for marked daily short equity."""

from dataclasses import replace

import pandas as pd
import pytest

from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_replay import ShortReplay, ShortTrade
from lab.bearish_equity import EquityAssumptions, daily_short_equity

COSTS = RoundTripCosts(5, 5, 2, 1)
BOUNDARY = pd.Timestamp("2026-05-08T00:00:00Z")


def candles(days=2):
    idx = pd.date_range("2026-01-01T00:01:00Z", periods=1440 * days, freq="min")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 98.0, "close": 100.0, "oi_invalid": False},
        index=idx,
    )


def trade(b, entry_bar, exit_bar, *, exit_ref=99.0):
    price = float(b.loc[entry_bar, "open"])
    spread = COSTS.spread_bps / 20_000
    slip = COSTS.slippage_per_side_bps / 10_000
    bid = price * (1 - spread)
    ask = exit_ref * (1 + spread)
    entry_fill, exit_fill = bid * (1 - slip), ask * (1 + slip)
    fee = COSTS.fee_per_side_bps / 10_000 * (entry_fill + exit_fill) / price
    slip_cost = (bid - entry_fill + exit_fill - ask) / price
    spread_cost = (price - bid + ask - exit_ref) / price
    gross = (price - exit_ref) / price
    reserve = COSTS.adverse_funding_bps / 10_000
    return ShortTrade(
        signal_end_ns=int(entry_bar.value - 2 * 60_000_000_000),
        entry_minute_start_ns=int(entry_bar.value - 60_000_000_000),
        exit_bar_end_ns=int(exit_bar.value),
        setup="breakdown",
        exit_reason="time",
        entry_reference=price,
        exit_reference=exit_ref,
        entry_fill=entry_fill,
        exit_fill=exit_fill,
        stop_reference=103.0,
        target_reference=98.0,
        gross_return=gross,
        net_return=(entry_fill - exit_fill) / price - fee - reserve,
        fee_cost=fee,
        slippage_cost=slip_cost,
        spread_cost=spread_cost,
        funding_reserve_cost=reserve,
    )


def replay(*trades, halted=False):
    return ShortReplay(None, tuple(trades), 0, 0, 0, halted, None)


@pytest.mark.parametrize("timestamp_unit", ["ns", "us"])
def test_daily_equity_normalizes_datetime_index_timestamp_units(timestamp_unit):
    frame = candles(days=1)
    frame.index = frame.index.as_unit(timestamp_unit)
    result = daily_short_equity(frame, replay(), COSTS, boundary=BOUNDARY)
    assert len(result.days) == 1 and result.flat_days == 1
    assert result.end_equity == 10_000
    missing = candles(days=2)
    missing.index = missing.index.as_unit(timestamp_unit)
    missing = missing.drop(missing.index[50])
    with pytest.raises(ValueError, match="minute-price gaps"):
        daily_short_equity(missing, replay(), COSTS, boundary=BOUNDARY)


def test_flat_utc_days_include_no_trade_days_with_zero_returns():
    result = daily_short_equity(candles(), replay(), COSTS, boundary=BOUNDARY)
    assert len(result.days) == 2
    assert result.end_equity == 10_000
    assert result.flat_days == 2
    assert result.total_return == 0
    assert all(d.net_pnl == 0 and not d.open_position for d in result.days)


def test_overnight_position_marked_at_midnight_and_realized_only_once():
    b = candles()
    entry = pd.Timestamp("2026-01-02T00:00:00Z")
    exit_ = pd.Timestamp("2026-01-02T00:03:00Z")
    b.loc[exit_, "close"] = 99.0
    t = trade(b, entry, exit_)
    result = daily_short_equity(b, replay(t), COSTS, boundary=BOUNDARY)
    first, second = result.days
    assert first.open_position and first.entries == 1 and first.exits == 0
    assert first.end_equity < 10_000 and first.realized_cash == 10_000
    assert first.unrealized_after_closeout < 0
    assert not second.open_position and second.entries == 0 and second.exits == 1
    assert second.end_equity == pytest.approx(10_000 * (1 + 0.25 * t.net_return))
    assert sum(d.net_pnl for d in result.days) == pytest.approx(result.end_equity - 10_000)
    assert result.end_equity > 10_000


def test_sequential_shorts_reinvest_only_realized_equity():
    b = candles()
    i1, x1 = pd.Timestamp("2026-01-01T02:00:00Z"), pd.Timestamp("2026-01-01T02:03:00Z")
    i2, x2 = pd.Timestamp("2026-01-02T02:00:00Z"), pd.Timestamp("2026-01-02T02:03:00Z")
    b.loc[[x1, x2], "close"] = 99.0
    t1, t2 = trade(b, i1, x1), trade(b, i2, x2)
    result = daily_short_equity(b, replay(t1, t2), COSTS, boundary=BOUNDARY)
    expected = 10_000 * (1 + 0.25 * t1.net_return) * (1 + 0.25 * t2.net_return)
    assert result.end_equity == pytest.approx(expected)
    assert [d.entries for d in result.days] == [1, 1]
    assert [d.exits for d in result.days] == [1, 1]


def test_missing_minutes_and_partial_utc_days_fail_closed():
    b = candles()
    with pytest.raises(ValueError, match="gaps"):
        daily_short_equity(b.drop(index=b.index[100]), replay(), COSTS, boundary=BOUNDARY)
    with pytest.raises(ValueError, match="full UTC days"):
        daily_short_equity(b.iloc[:-1], replay(), COSTS, boundary=BOUNDARY)
    with pytest.raises(ValueError, match="full UTC days"):
        daily_short_equity(b.iloc[1:], replay(), COSTS, boundary=BOUNDARY)


def test_tampered_trade_costs_or_halted_replay_rejected():
    b = candles()
    entry, exit_ = pd.Timestamp("2026-01-01T02:00:00Z"), pd.Timestamp("2026-01-01T02:03:00Z")
    b.loc[exit_, "close"] = 99.0
    t = trade(b, entry, exit_)
    with pytest.raises(ValueError, match="conservation"):
        daily_short_equity(b, replay(replace(t, fee_cost=t.fee_cost - 0.0001)), COSTS, boundary=BOUNDARY)
    with pytest.raises(ValueError, match="unresolved"):
        daily_short_equity(b, replay(t, halted=True), COSTS, boundary=BOUNDARY)


def test_untrusted_source_or_entry_price_mismatch_rejected():
    b = candles()
    entry, exit_ = pd.Timestamp("2026-01-01T02:00:00Z"), pd.Timestamp("2026-01-01T02:03:00Z")
    b.loc[exit_, "close"] = 99.0
    t = trade(b, entry, exit_)
    b.loc[pd.Timestamp("2026-01-01T02:02:00Z"), "oi_invalid"] = True
    with pytest.raises(ValueError, match="source quality"):
        daily_short_equity(b, replay(t), COSTS, boundary=BOUNDARY)
    b = candles()
    b.loc[exit_, "close"] = 99.0
    b.loc[entry, ["open", "high"]] = [102.0, 103.0]
    with pytest.raises(ValueError, match="entry reference"):
        daily_short_equity(b, replay(t), COSTS, boundary=BOUNDARY)


def test_overlapping_shorts_and_post_window_trade_rejected():
    b = candles()
    entry, exit_ = pd.Timestamp("2026-01-01T02:00:00Z"), pd.Timestamp("2026-01-01T02:03:00Z")
    b.loc[exit_, "close"] = 99.0
    t = trade(b, entry, exit_)
    with pytest.raises(ValueError, match="overlapping"):
        daily_short_equity(b, replay(t, t), COSTS, boundary=BOUNDARY)
    late_end = pd.Timestamp("2026-01-03T00:01:00Z")
    with pytest.raises(ValueError, match="beyond complete source"):
        daily_short_equity(b, replay(replace(t, exit_bar_end_ns=int(late_end.value))), COSTS, boundary=BOUNDARY)


def test_intrabar_high_spike_triggers_equity_floor_veto():
    b = candles()
    entry, exit_ = pd.Timestamp("2026-01-01T02:00:00Z"), pd.Timestamp("2026-01-01T02:03:00Z")
    b.loc[exit_, "close"] = 99.0
    b.loc[pd.Timestamp("2026-01-01T02:02:00Z"), "high"] = 1_000.0
    with pytest.raises(ValueError, match="equity floor"):
        daily_short_equity(
            b,
            replay(trade(b, entry, exit_)),
            COSTS,
            boundary=BOUNDARY,
            assumptions=EquityAssumptions(notional_fraction=1),
        )


def test_trailing_zero_trade_days_do_not_disappear_from_equity_history():
    b = candles(days=3)
    entry, exit_ = pd.Timestamp("2026-01-01T02:00:00Z"), pd.Timestamp("2026-01-01T02:03:00Z")
    b.loc[exit_, "close"] = 99.0
    result = daily_short_equity(b, replay(trade(b, entry, exit_)), COSTS, boundary=BOUNDARY)
    assert len(result.days) == 3
    assert result.days[0].exits == 1
    assert result.days[1].daily_return == 0
    assert result.days[2].daily_return == 0
    assert result.flat_days == 2 and result.positive_days == 1


def test_equity_rejects_holdout_or_missing_utc_boundary():
    b = candles()
    with pytest.raises(ValueError, match="protected holdout"):
        daily_short_equity(b, replay(), COSTS, boundary=b.index[-1].normalize())
    with pytest.raises(ValueError, match="UTC-midnight"):
        daily_short_equity(b, replay(), COSTS, boundary=pd.Timestamp("2026-05-08T01:00:00Z"))
