"""A live portfolio must never size new orders against a partly unmarked held book."""

from types import SimpleNamespace

from lab.agents.execution import ExecutionAgent


def _marks(held, prices, times, latest, *, live=True):
    self = SimpleNamespace(
        config=SimpleNamespace(live=live, borrow_book=None),
        held=held,
        prices=prices,
        price_stamps=times,
        price_ts=latest,
    )
    return ExecutionAgent._unsafe_held_marks(self)


def test_missing_and_stale_held_marks_are_rejected_before_equity_sizing():
    assert _marks({"GOOG": 10}, {}, {}, 0) == ["GOOG"]
    assert _marks({"GOOG": -10}, {"GOOG": 100}, {"GOOG": 1}, 2) == ["GOOG"]
    assert _marks({"GOOG": 10}, {"GOOG": float("nan")}, {"GOOG": 2}, 2) == ["GOOG"]
    assert _marks({"GOOG": 10}, {"GOOG": 0}, {"GOOG": 2}, 2) == ["GOOG"]
    assert _marks({"GOOG": -10}, {"GOOG": 100}, {"GOOG": 2}, 2) == []


def test_unheld_asset_may_be_missing_but_live_book_is_still_accurate():
    assert _marks({"GOOG": 10, "AAPL": 0}, {"GOOG": 100}, {"GOOG": 2}, 2) == []
    assert _marks({"GOOG": 10}, {}, {}, 0, live=False) == []
