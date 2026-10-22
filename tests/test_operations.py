"""Operational safety in the execution agent: kill switch, reconciliation, caps, smoke test."""

import dataclasses
import json
from datetime import date

import pytest

from lab import backtest
from lab.config import RiskSettings
from lab.safety import read_kill_switch, set_kill_switch
from lab.signals import BuyAndHold
from tests.conftest import staircase

NO_RISK = RiskSettings(1.0, 1.0, 0.9999)


def _run(cfg, tickers=("X",), **overrides):
    data = {t: staircase(12) for t in tickers}
    cfg = dataclasses.replace(cfg, warmup_start=date(2024, 1, 1))
    return backtest.run(
        cfg,
        backtest.equal_weight(BuyAndHold, list(tickers)),
        data,
        date(2024, 1, 2),
        None,
        risk=NO_RISK,
        execution_overrides=overrides,
    )


def test_kill_switch_halt_blocks_all_orders(goog_lab, tmp_path):
    kill = tmp_path / "KILL"
    set_kill_switch(kill, "halt", "test")
    r = _run(goog_lab, kill_path=str(kill))
    assert r.fills == [] and r.orders_sent == 0
    assert "kill_switch" in {e.kind for e in r.risk_events}


def test_kill_switch_flatten_never_buys(goog_lab, tmp_path):
    kill = tmp_path / "KILL"
    set_kill_switch(kill, "flatten", "test")
    r = _run(goog_lab, kill_path=str(kill))
    assert all(f.side == "SELL" for f in r.fills)


def test_unreadable_kill_file_still_halts(tmp_path):
    p = tmp_path / "KILL"
    p.write_text("garbage")
    assert read_kill_switch(p)["mode"] == "halt"


def test_ledger_broker_mismatch_blocks_trading(goog_lab, tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"cash": 50_000, "positions": {"X": 100}}))  # broker holds nothing
    r = _run(goog_lab, state_path=str(ledger))
    assert r.orders_sent == 0
    assert "reconciliation_mismatch" in {e.kind for e in r.risk_events}


def test_ledger_persists_positions_atomically(goog_lab, tmp_path):
    ledger = tmp_path / "ledger.json"
    r = _run(goog_lab, state_path=str(ledger))
    state = json.loads(ledger.read_text())
    sign = {"BUY": 1, "SELL": -1}
    assert state["positions"]["X"] == sum(sign[f.side] * f.qty for f in r.fills)
    assert state["cash"] == pytest.approx(
        r.equity.iloc[0] - sum(sign[f.side] * f.qty * f.price + f.commission for f in r.fills)
    )
    assert not ledger.with_suffix(".tmp").exists()


def test_daily_order_cap(goog_lab):
    r = _run(goog_lab, tickers=("X", "Y", "Z"), max_orders_per_day=1)
    first_day = {f.ts for f in r.fills}
    assert r.orders_sent >= 1
    assert "daily_cap" in {e.kind for e in r.risk_events}
    assert len({f.ticker for f in r.fills if f.ts == min(first_day)}) == 1


def test_order_value_cap(goog_lab):
    r = _run(goog_lab, max_order_value=5_000)
    assert max(f.qty * f.price for f in r.fills) <= 5_000 * 1.05  # next-open price can differ from the close
    assert "order_capped" in {e.kind for e in r.risk_events}


def test_paper_report_measures_implementation_shortfall(tmp_path):
    from lab.agents.messages import EquitySnapshot, FillReport, RiskEvent, SignalUpdate, to_dict
    from lab.paper_report import load_journal, summarize

    day = 86_400 * 10**9
    msgs = [
        SignalUpdate("s:X", "X", 1.0, 100.0, 1 * day),
        EquitySnapshot(1 * day, 100_000.0, 100_000.0, {}, 0.0),
        FillReport(2 * day, "X", "BUY", 100, 100.5, 1.0),  # 50 bps above the decision close: a cost
        SignalUpdate("s:X", "X", 0.0, 110.0, 3 * day),
        FillReport(4 * day, "X", "SELL", 100, 109.0, 1.0),  # 91 bps below: also a cost
        EquitySnapshot(3 * day, 101_000.0, 90_000.0, {"X": 100}, 0.1),
        RiskEvent(3 * day, "daily_cap", "x"),
    ]
    p = tmp_path / "journal-202610.jsonl"
    p.write_text("\n".join(json.dumps(to_dict(m)) for m in msgs))
    r = summarize(load_journal([p]))
    assert r["fills"] == 2 and r["commissions"] == 2.0
    assert r["shortfall_bps"]["mean"] == pytest.approx((50 + 90.909) / 2, abs=0.01)
    assert r["total_return"] == pytest.approx(0.01) and r["risk_events"] == {"daily_cap": 1}
