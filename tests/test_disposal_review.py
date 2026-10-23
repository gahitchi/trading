"""G010 follow-up: disposed intents remain attributable across fills and restarts."""

import json
import time
from types import MethodType, SimpleNamespace

import pytest
from nautilus_trader.model import OrderSide

from lab.agents.execution import _KEEP_TRADE_DAYS, ExecutionAgent


def _agent(tmp_path):
    a = SimpleNamespace(
        config=SimpleNamespace(live=True, dispose_path=str(tmp_path / "DISPOSE")),
        pending={},
        disposed={"X": {"ticker": "A", "side": "BUY", "qty": 10, "evidence": "checked"}},
        applied={},
        terminal={},
        persistence_failed=False,
        held={"A": 0.0},
        cash=1000.0,
        commissions=0.0,
        ledger_account="IB-DU1234567",
        halts=[],
        reports=[],
        orders={},
        _save=lambda: None,
        _paper_guard=lambda: True,
        _account_id=lambda: "IB-DU1234567",
        reconcile=lambda: [],
        _halt=lambda: None,
        _retry_liquidation=lambda: None,
    )
    a._hard_halt = lambda kind, detail: a.halts.append((kind, detail))
    a._risk_event = lambda kind, detail: a.reports.append((kind, detail))
    a.publish_message = lambda topic, value: None
    a.cache = SimpleNamespace(order=lambda coid: a.orders.get(str(coid)))
    for method in ("_apply_fill", "_close_pending", "_prune_applied", "_recover_pending", "_recovery_account_ok"):
        setattr(a, method, MethodType(getattr(ExecutionAgent, method), a))
    return a


def _fill(trade, qty):
    event = type("OrderFilled", (), {})()
    event.client_order_id = "X"
    event.trade_id = trade
    event.last_qty = qty
    event.last_px = 10
    event.commission = None
    event.order_side = OrderSide.BUY
    event.instrument_id = SimpleNamespace(symbol="A")
    event.ts_event = time.time_ns()
    return event


def test_disposed_partial_fills_keep_intent_until_terminal_report(tmp_path):
    a = _agent(tmp_path)
    order = SimpleNamespace(is_closed=False, events=lambda: [])
    a.orders["X"] = order
    first, second = _fill("T1", 4), _fill("T2", 6)
    ExecutionAgent.on_order_filled(a, first)
    assert "X" in a.pending  # another six shares may still fill
    assert "X" in a.disposed  # evidence is retained even after reactivation
    ExecutionAgent.on_order_filled(a, first)  # duplicate after reconnect
    order.is_closed = True
    ExecutionAgent.on_order_filled(a, second)
    assert a.held == {"A": 10} and a.cash == 900
    assert not a.pending and a.halts
    assert a.disposed["X"]["evidence"] == "checked"


def test_disposed_order_reappearing_during_restart_is_recovered(tmp_path):
    a = _agent(tmp_path)
    fill = _fill("T1", 10)
    a.orders["X"] = SimpleNamespace(is_closed=True, events=lambda: [fill])
    a._recover_pending()
    assert a.held == {"A": 10} and a.cash == 900 and a.halts
    a._recover_pending()
    assert a.held == {"A": 10} and a.cash == 900


def test_disposal_never_discards_older_evidence(tmp_path):
    a = _agent(tmp_path)
    original = dict(a.disposed["X"])
    a.disposed.update({f"OLD-{i}": dict(original) for i in range(500)})
    a.pending["NEW"] = {"ticker": "A", "side": "BUY", "qty": 10}
    (tmp_path / "DISPOSE").write_text(json.dumps({"orders": ["NEW"], "evidence": "checked"}))
    ExecutionAgent._consume_dispose(a)
    assert a.disposed["X"] == original and "NEW" in a.disposed


def test_pruning_preserves_disposed_and_legacy_trade_ids(tmp_path):
    a = _agent(tmp_path)
    old = time.time_ns() - (_KEEP_TRADE_DAYS + 1) * 86_400 * 10**9
    a.applied = {"T1": [old, "X"], "LEGACY": [old, None], "DONE": [old, "Y"]}
    a._prune_applied()
    assert set(a.applied) == {"T1", "LEGACY"}


@pytest.mark.parametrize("orders,evidence", [(["X"], None), ([{}], "text"), ("X", "text")])
def test_malformed_disposal_request_cannot_authorize_recovery(tmp_path, orders, evidence):
    a = _agent(tmp_path)
    a.pending["X"] = a.disposed.pop("X")
    (tmp_path / "DISPOSE").write_text(json.dumps({"orders": orders, "evidence": evidence}))
    ExecutionAgent._consume_dispose(a)
    assert "X" in a.pending and not a.disposed


def test_disposal_refuses_wrong_broker_account(tmp_path):
    a = _agent(tmp_path)
    a.pending["X"] = a.disposed.pop("X")
    a._paper_guard = lambda: False
    (tmp_path / "DISPOSE").write_text(json.dumps({"orders": ["X"], "evidence": "checked"}))
    ExecutionAgent._consume_dispose(a)
    assert "X" in a.pending and not a.disposed


def test_cancel_then_late_partial_fills_remain_attributable(tmp_path):
    a = _agent(tmp_path)
    a.pending["X"] = a.disposed.pop("X")
    a.orders["X"] = SimpleNamespace(is_closed=True, events=lambda: [])
    ExecutionAgent.on_order_canceled(a, SimpleNamespace(client_order_id="X"))
    assert "X" in a.terminal and not a.pending
    for event in (_fill("T1", 4), _fill("T2", 6), _fill("T1", 4)):
        ExecutionAgent.on_order_filled(a, event)
    assert a.cash == 900 and a.held["A"] == 10
    assert a.terminal["X"]["filled"] == 10 and a.halts


def test_terminal_callback_recovers_cached_fill_before_closing(tmp_path):
    a = _agent(tmp_path)
    a.pending["X"] = a.disposed.pop("X")
    fill = _fill("T1", 4)
    a.orders["X"] = SimpleNamespace(is_closed=True, events=lambda: [fill])
    ExecutionAgent.on_order_canceled(a, SimpleNamespace(client_order_id="X"))
    assert a.cash == 960 and a.terminal["X"]["filled"] == 4
    ExecutionAgent.on_order_filled(a, fill)
    assert a.cash == 960


def test_terminal_order_offline_late_fill_is_recovered_once(tmp_path):
    a = _agent(tmp_path)
    a.terminal["X"] = a.disposed.pop("X")
    fill = _fill("T1", 10)
    a.orders["X"] = SimpleNamespace(is_closed=True, events=lambda: [fill])
    a._recover_pending()
    a._recover_pending()
    assert a.cash == 900 and a.held["A"] == 10 and len(a.halts) == 1


@pytest.mark.parametrize("field,value", [("last_qty", float("nan")), ("last_px", -1), ("order_side", OrderSide.SELL)])
def test_invalid_fill_cannot_corrupt_cash_or_positions(tmp_path, field, value):
    a = _agent(tmp_path)
    a.pending["X"] = a.disposed.pop("X")
    fill = _fill("T1", 10)
    setattr(fill, field, value)
    assert not a._apply_fill(fill)
    assert a.cash == 1000 and a.held["A"] == 0 and not a.applied and a.halts


def test_disposal_refuses_known_partial_execution(tmp_path):
    a = _agent(tmp_path)
    a.pending["X"] = {**a.disposed.pop("X"), "filled": 4}
    (tmp_path / "DISPOSE").write_text(json.dumps({"orders": ["X"], "evidence": "no order seen"}))
    ExecutionAgent._consume_dispose(a)
    assert "X" in a.pending and not a.disposed


def test_disk_failure_stops_node_and_latches_submission_refusal(tmp_path):
    a = _agent(tmp_path)
    stopped = []
    a.shutdown_system = stopped.append
    a._persist_state = lambda: (_ for _ in ()).throw(OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        ExecutionAgent._save(a)
    assert a.persistence_failed and stopped
    assert not ExecutionAgent._submit(a, "A.XNAS", "A", 10)


@pytest.mark.parametrize("account", [None, "IB-DU7654321"])
def test_recovery_never_books_reports_into_another_account_ledger(tmp_path, account):
    a = _agent(tmp_path)
    a._account_id = lambda: account
    fill = _fill("T1", 10)
    a.orders["X"] = SimpleNamespace(is_closed=True, events=lambda: [fill])
    a._recover_pending()
    ExecutionAgent.on_order_filled(a, fill)
    assert a.cash == 1000 and a.held["A"] == 0 and not a.applied
    assert "X" in a.disposed and not a.pending
