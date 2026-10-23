"""Regressions for ChatGPT's G007 audit (F1, F3) and G004-F1, through the real agents."""

import json
from dataclasses import replace

import pandas as pd
import pytest

from lab import backtest
from lab.config import RiskSettings
from lab.halt import read_halt
from lab.signals.base import Signal
from lab.signals.registry import SignalSpec
from lab.sim.harness import LiveHarness

D0 = 556


class Short(Signal):
    name = "audit_short"

    def target(self, w):
        return -1.0


def _kinds(h):
    return [e["kind"] for e in h.journal() if e.get("type") == "RiskEvent"]


def _lab(h, *args):
    from lab.cli import main as lab_main

    return lab_main(["--config", str(h.cfg.root / "lab.toml"), *args])


# ---------------------------------------------------------------------------- F3: exits never need equity


def test_short_squeeze_through_zero_equity_is_covered_by_quantity(goog_lab):
    """G007-F3: equity went to -98k and the 990-share short was never covered."""
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e7},
        index=pd.bdate_range("2021-01-04", periods=20),
    )
    frame.loc[frame.index[8] :, ["open", "high", "low", "close"]] *= 3
    risk = RiskSettings(1, 1, 0.99, max_short_gross=1, min_net=-1, max_net=1)
    r = backtest.run(
        replace(goog_lab, risk=risk),
        [backtest.Sleeve(Short(), "GOOG", 1)],
        {"GOOG": frame},
        frame.index[1].date(),
        None,
    )
    path, pos = [], 0.0
    for f in r.fills:
        pos += f.qty if f.side == "BUY" else -f.qty
        path.append(pos)
    assert path[0] < 0 and pos == 0  # shorted, then covered completely
    assert all(p <= 0 for p in path)  # never reversed into a long
    assert "liquidation" in [e.kind for e in r.risk_events]


# ---------------------------------------------------------------------------- F1: cash


def test_underfunded_account_is_a_hard_halt_before_any_order(goog_lab, goog, tmp_path):
    """G007-F1: the broker held $1,000 and the ledger $100,000; an order went out anyway."""
    h = LiveHarness(
        goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0, broker_cash=1000
    )
    h.session()
    assert h.broker().state["orders"] == {}
    halt = read_halt(h.runtime / "HALT")
    assert halt["kind"] == "cash_mismatch" and "cannot fund" in halt["reason"]


def test_allocation_of_a_larger_account_trades(goog_lab, goog, tmp_path):
    """The default contract: the lab trades `capital` out of a bigger paper account (IBKR paper: ~$1M)."""
    h = LiveHarness(
        goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0, broker_cash=1_000_000
    )
    h.session()
    assert h.broker().positions() and read_halt(h.runtime / "HALT") is None


def _dedicated(cfg):
    return replace(cfg, paper=replace(cfg.paper, dedicated_account=True))


def test_dedicated_account_books_small_cash_moves_and_halts_on_large_ones(goog_lab, goog, tmp_path):
    h = LiveHarness(_dedicated(goog_lab), [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0)
    h.session()
    assert h.ledger()["synced_cash"] is True

    def dividend(b):
        b.state["cash"] += 37.5
        b.save()

    h.session(before_start=dividend)
    assert "external_cash" in _kinds(h)
    assert h.ledger()["cash"] == pytest.approx(h.broker().cash, abs=0.005)
    assert h.ledger()["external_cash"] == pytest.approx(37.5)

    def withdrawal(b):
        b.state["cash"] -= 20_000
        b.save()

    orders = len(h.broker().state["orders"])
    h.session(before_start=withdrawal)
    assert read_halt(h.runtime / "HALT")["kind"] == "cash_mismatch"
    assert len(h.broker().state["orders"]) == orders


def test_dedicated_account_that_never_matched_needs_an_operator(goog_lab, goog, tmp_path):
    h = LiveHarness(
        _dedicated(goog_lab),
        [SignalSpec("buy_and_hold")],
        {"GOOG": goog},
        tmp_path / "h",
        first_day=D0,
        broker_cash=1_000_000,
    )
    h.session()
    assert read_halt(h.runtime / "HALT")["kind"] == "cash_mismatch" and h.broker().state["orders"] == {}
    assert _lab(h, "recover", "--reason", "the lab owns the whole paper account", "--adopt-broker") == 0
    h.session()
    assert h.ledger()["cash"] == pytest.approx(h.broker().cash, abs=0.005) or h.broker().positions()
    assert read_halt(h.runtime / "HALT") is None and h.broker().positions()


# ---------------------------------------------------------------------------- G004-F1: an order nobody reports


def test_unreported_journaled_order_cannot_be_erased_by_adoption(goog_lab, goog, tmp_path):
    import time

    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0)
    ledger = h.runtime / "ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    coid = "O-20261022-000000-001-EXEC-9"
    pending = {coid: {"ticker": "GOOG", "side": "BUY", "qty": 10, "ts": time.time_ns()}}
    ledger.write_text(json.dumps({"cash": goog_lab.paper.capital, "positions": {}, "pending": pending}))
    h.session()
    assert "order_unknown" in _kinds(h) and h.broker().state["orders"] == {}
    halt = read_halt(h.runtime / "HALT")
    assert halt["kind"] == "reconciliation_mismatch" and "Elapsed time is not evidence" in halt["reason"]
    assert h.ledger()["pending"]  # the intent is kept, so a late fill would still be attributable
    assert _lab(h, "recover", "--reason", "IBKR shows no such order; it never left", "--adopt-broker") == 0
    h.session()
    assert read_halt(h.runtime / "HALT")["kind"] == "reconciliation_mismatch"  # (ChatGPT, G004-F1)
    assert h.ledger()["pending"] and not h.broker().positions()

    # Even days later, time cannot prove that an order never filled. Recovery must
    # retain its attribution until broker evidence resolves the uncertainty.
    state = json.loads(ledger.read_text())
    state["pending"][coid]["ts"] = time.time_ns() - 5 * 86_400 * 10**9
    ledger.write_text(json.dumps(state))
    assert _lab(h, "recover", "--reason", "IBKR shows no such order after its sessions ended", "--adopt-broker") == 0
    h.session()
    assert "order_expired_unreported" not in _kinds(h)
    assert read_halt(h.runtime / "HALT")["kind"] == "reconciliation_mismatch"
    assert coid in h.ledger()["pending"] and not h.broker().positions()


def test_an_unconfirmed_order_is_disposed_only_on_recorded_broker_evidence(goog_lab, goog, tmp_path):
    """Claude on G010 / G004-F1: the terminal-resolution workflow. The operator reads IBKR's order and trade
    history and records what it shows; the intent moves to `disposed` (never erased) and trading resumes."""
    import time

    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0)
    ledger = h.runtime / "ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    coid = "O-20261022-000000-001-EXEC-9"
    pending = {coid: {"ticker": "GOOG", "side": "BUY", "qty": 10, "ts": time.time_ns()}}
    ledger.write_text(json.dumps({"cash": goog_lab.paper.capital, "positions": {}, "pending": pending}))
    h.session()
    assert read_halt(h.runtime / "HALT")["kind"] == "reconciliation_mismatch"
    with pytest.raises(SystemExit, match="needs --evidence"):
        _lab(h, "recover", "--reason", "checked", "--dispose-order", coid)
    evidence = "TWS Trades and Orders for DU1234567, 2026-10-22..23: no order or execution with this ID"
    assert _lab(h, "recover", "--reason", "never reached IBKR", "--dispose-order", coid, "--evidence", evidence) == 0
    h.session()
    kinds = _kinds(h)
    assert "order_disposed" in kinds and read_halt(h.runtime / "HALT") is None
    state = h.ledger()
    assert state["pending"] == {} and state["disposed"][coid]["evidence"] == evidence  # kept, with its evidence
    assert h.broker().positions()  # trading resumed


def test_disposal_is_refused_while_the_position_disagrees():
    from types import SimpleNamespace

    from lab.agents.execution import ExecutionAgent

    events = []
    agent = SimpleNamespace(
        config=SimpleNamespace(dispose_path=None, live=True),
        _paper_guard=lambda: True,
        _account_id=lambda: "IB-DU1234567",
        ledger_account="IB-DU1234567",
        applied={},
        pending={"X": {"ticker": "GOOG", "side": "BUY", "qty": 10}},
        disposed={},
        reconcile=lambda: ["GOOG: ledger 0 vs broker 10"],
        cache=SimpleNamespace(order=lambda _id: None),
        _risk_event=lambda kind, detail: events.append(kind),
        _save=lambda: None,
    )
    import tempfile
    from pathlib import Path

    req = Path(tempfile.mkdtemp()) / "DISPOSE"
    req.write_text(json.dumps({"orders": ["X"], "evidence": "none seen", "reason": "r"}))
    agent.config.dispose_path = str(req)
    ExecutionAgent._consume_dispose(agent)
    assert events == ["dispose_refused"] and "X" in agent.pending and not agent.disposed and not req.exists()


# ---------------------------------------------------------------------------- F15: who owns the state


def test_one_paper_node_per_runtime_directory(goog_lab):
    from lab.paper import RuntimeLocked, acquire_runtime_lock

    first = acquire_runtime_lock(goog_lab)
    with pytest.raises(RuntimeLocked, match="another paper node"):
        acquire_runtime_lock(goog_lab)
    first.close()  # released (also by the OS when a process dies)
    acquire_runtime_lock(goog_lab).close()


@pytest.mark.parametrize(
    "state, reason",
    [
        ({"account": "IB-DU7777777"}, "broker reports belong to another ledger account"),
        ({"positions": {"TSLA": 10}}, "not in this node's universe"),
    ],
)
def test_a_ledger_from_another_account_or_universe_is_refused(goog_lab, goog, tmp_path, state, reason):
    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0)
    ledger = h.runtime / "ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"cash": goog_lab.paper.capital, "positions": {}, **state}))
    h.session()
    halt = read_halt(h.runtime / "HALT")
    assert halt["kind"] == "reconciliation_mismatch" and reason in halt["reason"]
    assert h.broker().state["orders"] == {}
    if "account" in state:
        persisted = json.loads(ledger.read_text())
        assert persisted["account"] == state["account"]
        assert persisted["cash"] == goog_lab.paper.capital and persisted["positions"] == {}
    if "positions" in state:
        assert json.loads(ledger.read_text())["positions"].get("TSLA") == 10  # kept, never dropped


def test_a_fill_for_a_disposed_order_is_booked_and_halts(tmp_path):
    from lab.agents.execution import ExecutionAgent
    from tests.test_disposal_review import _agent, _fill

    agent = _agent(tmp_path)
    ExecutionAgent.on_order_filled(agent, _fill("T1", 4))
    assert agent.held["A"] == 4 and agent.halts[0][0] == "reconciliation_mismatch"
    assert "X" in agent.disposed and "X" in agent.pending  # remaining quantity still attributable


def test_the_trade_id_store_is_bounded_but_keeps_ids_of_journaled_orders():
    """Claude on G009-F27: de-duplication forgets a trade ID only after the broker's re-report window and once
    its order is no longer journaled."""
    import time
    from types import SimpleNamespace

    from lab.agents.execution import _KEEP_TRADE_DAYS, ExecutionAgent

    old = time.time_ns() - (_KEEP_TRADE_DAYS + 1) * 86_400 * 10**9
    agent = SimpleNamespace(
        pending={"STILL-OPEN": {}},
        disposed={},
        terminal={},
        applied={"T1": [old, "DONE"], "T2": [old, "STILL-OPEN"], "T3": [time.time_ns(), "DONE"]},
    )
    ExecutionAgent._prune_applied(agent)
    assert set(agent.applied) == {"T2", "T3"}
