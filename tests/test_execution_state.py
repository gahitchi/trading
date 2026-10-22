"""Order intent and checkpoint regression tests independent of the live broker."""

import pytest

from lab.execution_state import ExecutionStateError, apply_order_fill, read_execution_state


def test_execution_checkpoint_rejects_legacy_live_state():
    checkpoint = {"cash": 1000, "positions": {"X": 1}}
    with pytest.raises(ExecutionStateError, match="legacy"):
        read_execution_state(checkpoint, {"X"}, live=True)
    assert read_execution_state(checkpoint, {"X"}, live=False)["cash"] == 1000


@pytest.mark.parametrize(
    "broken",
    [
        {"cash": float("nan")},
        {"cash": 100, "positions": {"UNKNOWN": 1}},
        {"cash": 100, "daily_usage": {"session": "", "orders": -1, "bought": 0}},
        {"cash": 100, "processed_fills": ["same", "same"]},
        {"cash": 100, "safety_halt": "no"},
        {"cash": 100, "unresolved_orders": {"o1": {"ticker": "X", "side": "BUY", "quantity": 1, "filled": 2}}},
    ],
)
def test_checkpoint_rejects_inconsistent_state(broken):
    with pytest.raises(ExecutionStateError):
        read_execution_state(broken, {"X"})


def test_order_fill_persisted_identity_prevents_double_booking():
    pending = {"o1": {"ticker": "X", "side": "BUY", "quantity": 10.0, "filled": 0.0}}
    seen = set()
    assert apply_order_fill(pending, seen, "o1", "trade-a", 4) is True
    assert pending["o1"]["filled"] == 4
    assert apply_order_fill(pending, seen, "o1", "trade-a", 4) is False
    assert pending["o1"]["filled"] == 4
    assert apply_order_fill(pending, seen, "o1", "trade-b", 6) is True
    assert not pending
    assert apply_order_fill(pending, seen, "o1", "trade-b", 6) is False


def test_unknown_fill_never_creates_a_synthetic_order():
    with pytest.raises(ExecutionStateError, match="unknown order"):
        apply_order_fill({}, set(), "not-recorded", "fill-a", 3)


def test_overfill_is_rejected_before_mutating_state():
    pending = {"o1": {"ticker": "X", "side": "BUY", "quantity": 5.0, "filled": 4.0}}
    seen = set()
    with pytest.raises(ExecutionStateError, match="exceeds"):
        apply_order_fill(pending, seen, "o1", "fill-b", 2.0)
    assert pending["o1"]["filled"] == 4.0
    assert not seen


def test_processed_fills_survive_checkpoint_restore():
    raw = {
        "version": 2,
        "cash": 1000.0,
        "positions": {"X": 4.0},
        "unresolved_orders": {"o1": {"ticker": "X", "side": "BUY", "quantity": 10, "filled": 4}},
        "daily_usage": {"session": "2026-10-22", "orders": 1, "bought": 1000},
        "processed_fills": ["o1:trade-a"],
        "safety_halt": False,
    }
    state = read_execution_state(raw, {"X"}, live=True)
    assert apply_order_fill(state["unresolved_orders"], state["processed_fills"], "o1", "trade-a", 4) is False
    assert state["unresolved_orders"]["o1"]["filled"] == 4
