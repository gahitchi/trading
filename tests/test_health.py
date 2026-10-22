"""Operational health must not treat a recent timestamp as broker safety."""

from lab.health import assess_heartbeat


def _hb(**overrides):
    hb = {
        "paper_account_ok": True,
        "kill_switch": None,
        "halted": False,
        "safety_halt": False,
        "unresolved_orders": 0,
        "reconciliation_issues": [],
        "ready": False,
    }
    hb.update(overrides)
    return hb


def test_healthy_but_not_order_ready_before_daily_snapshot():
    assert assess_heartbeat(_hb(), 4, 60, risk_halted=False) == []


def test_recent_heartbeat_with_wrong_broker_is_unhealthy():
    result = assess_heartbeat(_hb(paper_account_ok=False), 1, 60, risk_halted=False)
    assert "verified paper account not available" in result


def test_critical_reconciliation_errors_are_unhealthy():
    result = assess_heartbeat(
        _hb(reconciliation_issues=["unmanaged broker position BAD.XNAS"]), 1, 60, risk_halted=False
    )
    assert "unresolved broker position or cache mismatch" in result


def test_open_orders_are_visible_without_falsely_claiming_new_order_readiness():
    hb = _hb(reconciliation_issues=["outstanding broker order O-123"], unresolved_orders=1, ready=False)
    assert assess_heartbeat(hb, 1, 60, risk_halted=False) == []


def test_risk_halt_and_corrupt_status_fail_closed():
    assert "persistent safety/risk halt" in assess_heartbeat(_hb(), 1, 60, risk_halted=True)
    assert assess_heartbeat({"at": "today"}, 1, 60, risk_halted=False)
    assert assess_heartbeat(_hb(), -50, 60, risk_halted=False)
    assert assess_heartbeat(None, None, 60, risk_halted=False)
