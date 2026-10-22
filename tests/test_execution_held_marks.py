"""A live portfolio must never size orders against a partly unmarked or stale book.

Held-mark cases by ChatGPT (G004-F5); the session and market-clock cases answer G007-F2.
"""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from lab.agents.execution import ExecutionAgent
from lab.agents.schedule import NY
from lab.agents.signal_agent import SignalAgent

NOW = datetime(2026, 10, 22, 17, 0, tzinfo=NY)  # a Thursday, after the close


def _ts(day: str) -> int:
    return int(pd.Timestamp(f"{day} 16:00", tz=NY).value)


class _Clock:
    def __init__(self, now: datetime):
        self.now = now

    def timestamp_ns(self) -> int:
        return int(self.now.timestamp() * 1e9)


def _marks(held, prices, times, latest, *, live=True, now=NOW):
    self = SimpleNamespace(
        config=SimpleNamespace(live=live, borrow_book=None, market_offset_days=0.0, max_bar_age_days=4.0),
        held=held,
        prices=prices,
        price_stamps=times,
        price_ts=latest,
        clock=_Clock(now),
    )
    return ExecutionAgent._unsafe_held_marks(self)


TODAY, YESTERDAY = _ts("2026-10-22"), _ts("2026-10-21")


def test_missing_and_stale_held_marks_are_rejected_before_equity_sizing():
    assert _marks({"GOOG": 10}, {}, {}, 0) == ["<no prices yet>"]
    assert _marks({"GOOG": -10}, {"GOOG": 100}, {"GOOG": YESTERDAY}, TODAY) == ["GOOG"]  # an older session
    assert _marks({"GOOG": 10}, {"GOOG": float("nan")}, {"GOOG": TODAY}, TODAY) == ["GOOG"]
    assert _marks({"GOOG": 10}, {"GOOG": 0}, {"GOOG": TODAY}, TODAY) == ["GOOG"]
    assert _marks({"GOOG": -10}, {"GOOG": 100}, {"GOOG": TODAY}, TODAY) == []


def test_unheld_asset_may_be_missing_but_live_book_is_still_accurate():
    assert _marks({"GOOG": 10, "AAPL": 0}, {"GOOG": 100}, {"GOOG": TODAY}, TODAY) == []
    assert _marks({"GOOG": 10}, {}, {}, 0, live=False) == []


def test_two_equally_stale_marks_do_not_agree():
    """G007-F2: a held mark from October 1 matched an October 1 book and passed."""
    old = _ts("2026-10-01")
    assert _marks({"GOOG": 10}, {"GOOG": 100}, {"GOOG": old}, old)[0].startswith("<newest price:")
    future = _ts("2026-11-01")
    assert "future" in _marks({"GOOG": 10}, {"GOOG": 100}, {"GOOG": future}, future)[0]
    # A long weekend is fine: Friday's close on Tuesday morning is under the 4-day limit.
    tuesday = datetime(2026, 10, 27, 9, 0, tzinfo=NY)
    friday = _ts("2026-10-23")
    assert _marks({"GOOG": 10}, {"GOOG": 100}, {"GOOG": friday}, friday, now=tuesday) == []


def _intake(stamps, now=NOW):
    published, events = [], []
    agent = SimpleNamespace(
        config=SimpleNamespace(session_close="16:00", market_offset_days=0.0, max_bar_age_days=4.0, ticker="GOOG"),
        clock=_Clock(now),
        last_ts=-1,
        errors=0,
        _process=lambda b, publish: published.append((b.ts_event, publish)),
        _event=lambda kind, detail: events.append(kind),
    )
    SignalAgent.on_historical_bars(agent, [SimpleNamespace(ts_event=s) for s in stamps])
    return published, events


def test_signal_intake_ignores_future_bars_and_does_not_act_on_a_stale_batch():
    old, future = _ts("2026-10-01"), _ts("2026-11-01")
    published, events = _intake([old, future])
    assert published == [(old, False)]  # the future bar is never seen; the old one is history only
    assert events == ["future_bar", "stale_data"]
    published, events = _intake([YESTERDAY, TODAY])
    assert published == [(YESTERDAY, False), (TODAY, True)] and events == []
    # Today's bar before the close is still forming.
    published, _ = _intake([YESTERDAY, TODAY], now=datetime(2026, 10, 22, 15, 0, tzinfo=NY))
    assert published == [(YESTERDAY, True)]
