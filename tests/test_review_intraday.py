"""Adversarial cases for Claude's C003 release: timestamps, stale books and independent exits."""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from nautilus_trader.model import Bar, Price, Quantity

from lab import experiments
from lab.agents.broker_bars import normalize_ib_bar
from lab.agents.execution import ExecutionAgent
from lab.agents.schedule import NY, Session
from lab.signals.registry import SignalSpec
from lab.sim.intraday import IntradayHarness
from lab.universe import bar_type, instrument_id
from tests.test_intraday_backtest import frames, lab
from tests.test_intraday_harness import _cfg, _frames


def test_ib_daily_date_is_a_session_close_not_a_future_utc_midnight():
    stamp = int(pd.Timestamp("2026-10-22T23:59:59.999999999Z").value)
    b = Bar(bar_type(instrument_id("GOOG")), *[Price(100, 2)] * 4, Quantity(100, 0), stamp, stamp)
    normalized = normalize_ib_bar(b)
    assert datetime.fromtimestamp(normalized.ts_event / 1e9, NY) == datetime(2026, 10, 22, 16, tzinfo=NY)
    assert normalized.close == b.close and normalized.ts_init == b.ts_init


def test_last_hourly_ib_bar_is_cut_at_regular_close():
    stamp = int(pd.Timestamp("2026-10-22 16:30", tz=NY).value)
    b = Bar(bar_type(instrument_id("GOOG"), "1-HOUR"), *[Price(100, 2)] * 4, Quantity(100, 0), stamp, stamp)
    assert datetime.fromtimestamp(normalize_ib_bar(b, Session(60)).ts_event / 1e9, NY).hour == 16
    assert datetime.fromtimestamp(normalize_ib_bar(b, Session(60)).ts_event / 1e9, NY).minute == 0


def test_intraday_held_and_target_marks_must_match_the_current_bar():
    now = datetime(2026, 10, 22, 12, tzinfo=NY)
    fresh = int(now.timestamp() * 1e9)
    old = int(pd.Timestamp("2026-10-22 10:00", tz=NY).value)
    c = SimpleNamespace(live=True, borrow_book=None, session=Session(15), market_offset_days=0, max_bar_age_days=1)
    agent = SimpleNamespace(
        config=c,
        weights={"A": 0.5, "B": 0.5},
        held={"A": 1, "B": 0},
        prices={"A": 100, "B": 100},
        price_stamps={"A": old, "B": fresh},
        price_ts=fresh,
        clock=SimpleNamespace(timestamp_ns=lambda: fresh),
    )
    assert ExecutionAgent._unsafe_held_marks(agent) == ["A"]
    agent.price_stamps = {"A": fresh, "B": old}
    assert ExecutionAgent._unsafe_held_marks(agent) == ["B"]  # stale unheld target is unsafe too


def test_intraday_holdout_never_becomes_training_after_a_fetch(goog_lab):
    cfg = lab(goog_lab)
    initial = {"GOOG": frames(days=pd.bdate_range("2026-03-02", periods=10))}
    expanded = {"GOOG": frames(days=pd.bdate_range("2026-03-02", periods=20))}
    _, reserved = experiments.intraday_split(cfg, initial)
    research, later = experiments.intraday_split(cfg, expanded)
    assert reserved.start == later.start and research.end < reserved.start
    with pytest.raises(experiments.HoldoutUsed):
        experiments.guard_intraday(cfg, reserved, expanded)


def test_flat_at_close_survives_a_lost_data_feed(goog_lab, tmp_path):
    h = IntradayHarness(_cfg(goog_lab), [SignalSpec("buy_and_hold")], _frames(), tmp_path / "h", first_day=2)
    original = h.market.bars

    def interrupted(ticker, bt, start_ns):
        if h.market.session.minute_of(datetime.now(NY)) > 180:
            return []  # after entry, data stops for the rest of the session
        return original(ticker, bt, start_ns)

    h.market.bars = interrupted
    result = h.session()
    assert result.error is None
    assert any(f.side == "BUY" for f in result.fills)
    assert any(f.side == "SELL" for f in result.fills)
    assert not result.broker_positions
    assert any(e["kind"] == "flat_at_close" for e in result.risk_events)
