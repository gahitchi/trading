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
    assert halt["kind"] == "reconciliation_mismatch" and "can no longer fill after" in halt["reason"]
    assert h.ledger()["pending"]  # the intent is kept, so a late fill would still be attributable
    assert _lab(h, "recover", "--reason", "IBKR shows no such order; it never left", "--adopt-broker") == 0
    h.session()
    assert read_halt(h.runtime / "HALT")["kind"] == "reconciliation_mismatch"  # (ChatGPT, G004-F1)
    assert h.ledger()["pending"] and not h.broker().positions()

    # Days later the order's sessions are over and IBKR never reported it: it can no longer fill. An audited
    # recover lets the next start dispose of it, and trading resumes (Claude: no journal surgery needed).
    state = json.loads(ledger.read_text())
    state["pending"][coid]["ts"] = time.time_ns() - 5 * 86_400 * 10**9
    ledger.write_text(json.dumps(state))
    assert _lab(h, "recover", "--reason", "IBKR shows no such order after its sessions ended", "--adopt-broker") == 0
    h.session()
    assert "order_expired_unreported" in _kinds(h)
    assert read_halt(h.runtime / "HALT") is None and h.ledger()["pending"] == {} and h.broker().positions()


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
        ({"account": "IB-DU7777777"}, "belongs to account IB-DU7777777"),
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
    if "positions" in state:
        assert json.loads(ledger.read_text())["positions"].get("TSLA") == 10  # kept, never dropped
