"""A missing reconciliation report cannot erase a journaled order intent."""

from lab.agents.execution import ExecutionAgent


class _Cache:
    def order(self, _order_id):
        return None


class _Agent:
    _order_expiry = ExecutionAgent._order_expiry

    def __init__(self):
        from types import SimpleNamespace

        self.config = SimpleNamespace(live=True)
        self.pending = {"O-123": {"ticker": "GOOG", "side": "BUY", "qty": 10}}  # no submission time: never expires
        self.cache = _Cache()
        self.events = []
        self.halts = []
        self.active_halt = None

    def _risk_event(self, kind, detail):
        self.events.append((kind, detail))

    def _halt(self):
        return self.active_halt

    def _hard_halt(self, kind, detail):
        self.halts.append((kind, detail))
        self.active_halt = {"kind": kind, "reason": detail}

    def _close_pending(self, _coid):
        raise AssertionError("no journaled intent may be erased without a conclusive broker report")


def test_inconclusive_broker_order_report_keeps_intent_and_halts():
    node = _Agent()
    ExecutionAgent._recover_pending(node)
    assert "O-123" in node.pending
    assert len(node.halts) == 1
    assert node.halts[0][0] == "reconciliation_mismatch"
    assert node.events[0][0] == "order_unknown"

    # A heartbeat must not erase the intent or flood the hard-halt history.
    ExecutionAgent._recover_pending(node)
    assert "O-123" in node.pending
    assert len(node.halts) == 1
