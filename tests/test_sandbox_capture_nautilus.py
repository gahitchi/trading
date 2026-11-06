"""NautilusTrader-backed sandbox capture integration; skipped only without optional engine."""

import os
from decimal import Decimal
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from lab.safety import read_kill_switch
from lab.sandbox_capture import PaperOrderEventCapture
from tests.test_sandbox_capture import IID, NS, event


def require_nautilus():
    """Portable users may skip; GitHub CI must test the actual engine."""
    if find_spec("nautilus_trader") is None:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail("CI must install nautilus_trader to verify sandbox capture integration", pytrace=False)
        pytest.skip("NautilusTrader is not installed; engine-only integration test", allow_module_level=False)


def test_capture_failure_halts_risk_and_writes_kill_file(tmp_path):
    require_nautilus()

    from lab.fast import FastStrategy
    from lab.risk_manager import TradingState

    class FailingCapture:
        def append(self, *_args, **_kwargs):
            raise OSError("disk full")

    risk = SimpleNamespace(_state_error=None, state=TradingState.ACTIVE, reason="")
    kill = tmp_path / "KILL"
    stub = SimpleNamespace(
        paper_capture=FailingCapture(),
        clock=SimpleNamespace(timestamp_ns=lambda: NS + 2),
        risk=risk,
        _paper_capture_kill_file=str(kill),
    )
    with pytest.raises(RuntimeError, match="paper order evidence capture failed"):
        FastStrategy.on_order_event(stub, event())
    assert risk.state == TradingState.HALTED
    assert risk._state_error is not None
    assert read_kill_switch(kill)["mode"] == "halt"


def test_real_nautilus_backtest_captures_order_events_without_network(tmp_path):
    require_nautilus()

    from lab.fast import backtest
    from tests.test_maker import catalog, cfg, ranging_market

    path = catalog(tmp_path / "catalog", *ranging_market(steps=6))
    target = tmp_path / "orders.jsonl"
    result = backtest(
        path,
        cfg(),
        latency_ms=0,
        maker_bps=0,
        capture_path=str(target),
        capture_session="synthetic-run",
    )
    archive = PaperOrderEventCapture(target, session_id="synthetic-run", instrument_id=IID)
    order_events = archive.receipts()
    fills = [r for r in order_events if r["kind"] == "OrderFilled"]
    assert result["fills"] > 0
    assert len(fills) == result["fills"]
    assert all(r["trade_id"] and r["event_id"] for r in fills)
    assert all(Decimal(r["last_qty"]) > 0 for r in fills)
    # Only this self-contained, flat-start synthetic session qualifies
    # for fill-delta comparison, not real margin/cash reconciliation.
    expected_qty = Decimal(str(result["terminal"]["net_qty"]))
    assert Decimal(archive.compare_flat_start_positions({IID: str(expected_qty)})[IID]) == expected_qty
