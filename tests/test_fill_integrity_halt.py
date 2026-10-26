"""Adversarial partial-fill, overfill and invalid-report accounting regressions.

All events and brokerage objects are local fakes; no paper broker required.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from nautilus_trader.model import OrderSide

from lab.agents.execution import ExecutionAgent
from lab.execution_state import validate_ledger
from lab.execution_store import ExecutionStore
from tests.test_disposal_review import _agent, _fill


def active(tmp_path, *, closed=False):
    agent = _agent(tmp_path)
    agent.pending["X"] = agent.disposed.pop("X")
    agent.orders["X"] = SimpleNamespace(is_closed=closed, events=lambda: [])
    return agent


def test_overfill_books_actual_quantity_once_and_halts_without_clipping(tmp_path):
    agent = active(tmp_path)
    snapshots = []
    agent._save = lambda: snapshots.append(deepcopy(agent.pending))
    ExecutionAgent.on_order_filled(agent, _fill("T1", 8))
    assert agent.held["A"] == 8
    assert agent.cash == 920
    assert not agent.halts
    ExecutionAgent.on_order_filled(agent, _fill("T2", 3))
    assert agent.held["A"] == 11 and agent.cash == 890
    assert agent.pending["X"]["filled"] == 11
    assert agent.pending["X"]["overfilled"] is True
    assert agent.applied["T1"][1] == agent.applied["T2"][1] == "X"
    assert snapshots[-1]["X"]["overfilled"] is True
    assert agent.halts[-1][0] == "reconciliation_mismatch"
    assert "11 filled vs 10 submitted" in agent.halts[-1][1]
    ExecutionAgent.on_order_filled(agent, _fill("T2", 3))
    assert agent.held["A"] == 11 and agent.cash == 890
    assert len(agent.applied) == 2 and len(agent.halts) == 1


def test_exact_cumulative_fill_does_not_trigger_overfill(tmp_path):
    agent = active(tmp_path)
    ExecutionAgent.on_order_filled(agent, _fill("T1", 4))
    ExecutionAgent.on_order_filled(agent, _fill("T2", 6))
    assert agent.pending["X"]["filled"] == 10
    assert not agent.pending["X"].get("overfilled")
    assert not agent.halts


def test_tiny_floating_roundoff_never_triggers_false_overfill(tmp_path):
    agent = active(tmp_path)
    ExecutionAgent.on_order_filled(agent, _fill("T1", 10.0000000002))
    assert agent.pending["X"]["filled"] == pytest.approx(10.0000000002)
    assert not agent.pending["X"].get("overfilled")
    assert not agent.halts


def test_cached_invalid_terminal_fill_keeps_unsettled_intent(tmp_path):
    agent = active(tmp_path, closed=True)
    bad = _fill("T1", 4)
    bad.order_side = OrderSide.SELL
    agent.orders["X"].events = lambda: [bad]
    ExecutionAgent.on_order_filled(agent, bad)
    assert "X" in agent.pending and "X" not in agent.terminal
    assert agent.pending["X"]["invalid_fill"] is True
    assert agent.cash == 1000 and agent.held["A"] == 0 and not agent.applied
    ExecutionAgent.on_order_canceled(agent, SimpleNamespace(client_order_id="X"))
    assert "X" in agent.pending and not agent.terminal
    agent._recover_pending()
    assert "X" in agent.pending and agent.pending["X"]["invalid_fill"]
    assert agent.cash == 1000 and agent.held["A"] == 0
    assert agent.halts and all(kind == "reconciliation_mismatch" for kind, _ in agent.halts)


def test_invalid_fill_latch_survives_sqlite_restart(tmp_path):
    agent = active(tmp_path, closed=True)
    store = ExecutionStore(tmp_path / "ledger.json")
    agent._save = lambda: store.save({"cash": agent.cash, "positions": agent.held, "pending": agent.pending})
    bad = _fill("T1", 10)
    bad.last_px = float("nan")
    ExecutionAgent.on_order_filled(agent, bad)
    assert agent.cash == 1000 and agent.held["A"] == 0
    restarted = ExecutionStore(tmp_path / "ledger.json")
    state = restarted.load()
    assert state["pending"]["X"]["invalid_fill"] is True
    assert "X" not in state.get("terminal", {})
    validate_ledger(state)


def test_trade_id_collision_with_another_order_halts_without_duplicate_booking(tmp_path):
    agent = active(tmp_path)
    ExecutionAgent.on_order_filled(agent, _fill("T1", 4))
    fraud = _fill("T1", 9)
    fraud.client_order_id = "OTHER"
    ExecutionAgent.on_order_filled(agent, fraud)
    assert agent.held["A"] == 4 and agent.cash == 960
    assert len(agent.applied) == 1
    assert agent.halts[-1][0] == "reconciliation_mismatch"
    assert "multiple order IDs" in agent.halts[-1][1]


def test_terminal_callback_rechecks_trade_id_conflict(tmp_path):
    agent = active(tmp_path)
    ExecutionAgent.on_order_filled(agent, _fill("T1", 4))
    agent.pending["Y"] = {"ticker": "A", "side": "BUY", "qty": 5}
    wrong = _fill("T1", 5)
    wrong.client_order_id = "Y"
    agent.orders["Y"] = SimpleNamespace(is_closed=True, events=lambda: [wrong])
    ExecutionAgent.on_order_canceled(agent, SimpleNamespace(client_order_id="Y"))
    assert "Y" in agent.pending and "Y" not in agent.terminal
    assert agent.cash == 960 and agent.held["A"] == 4
    assert agent.halts and "multiple order IDs" in agent.halts[-1][1]


@pytest.mark.parametrize("name,value", [("invalid_fill", 1), ("overfilled", "true")])
def test_persisted_anomaly_flags_require_strict_booleans(name, value):
    with pytest.raises(ValueError, match=name):
        validate_ledger({"cash": 100, "pending": {"X": {"ticker": "A", "side": "BUY", "qty": 10, name: value}}})


def test_valid_terminal_partial_fill_is_still_recovered_and_archived(tmp_path):
    agent = active(tmp_path, closed=True)
    fill = _fill("T1", 4)
    agent.orders["X"].events = lambda: [fill]
    ExecutionAgent.on_order_canceled(agent, SimpleNamespace(client_order_id="X"))
    assert "X" in agent.terminal and "X" not in agent.pending
    assert agent.terminal["X"]["filled"] == 4
    assert agent.held["A"] == 4 and agent.cash == 960
    assert len(agent.applied) == 1 and not agent.halts
