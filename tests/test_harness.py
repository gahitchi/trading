"""The paper-trading node, end to end, against the simulated IBKR paper broker (lab.sim).

Each harness session is a full trading day through the real LiveNode (about 7 s of wall
clock), so these tests are slow by unit-test standards; they are the tests of what
actually runs against the broker.
"""

import json
from datetime import date

import pytest

from lab import backtest, data
from lab.agents.schedule import NY, next_at, parse_clock
from lab.safety import set_kill_switch
from lab.signals.registry import SignalSpec
from lab.sim.broker import SimBroker
from lab.sim.harness import LiveHarness

# GOOG, 1 Nov 2006: sma_trend(3, 10) enters and then flips three times in seven sessions.
D0 = 556
TREND = SignalSpec("sma_trend", {"fast": 3, "slow": 10})


@pytest.fixture
def frames():
    return {"GOOG": data.validate(data.sample_goog(), "GOOG")}


def _harness(cfg, frames, tmp_path, spec=TREND, **kw):
    return LiveHarness(cfg, [spec], frames, tmp_path / "harness", first_day=D0, **kw)


def _assert_books_agree(h):
    """The lab's ledger equals the broker's account: cash to the cent, every position exactly."""
    ledger, broker = h.ledger(), h.broker()
    assert ledger["positions"] == broker.positions()
    assert ledger["cash"] == pytest.approx(broker.cash, abs=0.005)
    assert ledger["pending"] == {}


def _kinds(h):
    return [e["kind"] for e in h.journal() if e.get("type") == "RiskEvent"]


# ---------------------------------------------------------------------------- schedule (fast)


def test_schedule_accepts_seconds_and_skips_weekends():
    from datetime import datetime

    assert parse_clock("16:15").second == 0 and parse_clock("16:15:07").second == 7
    with pytest.raises(ValueError):
        parse_clock("4pm")
    friday = datetime(2026, 10, 23, 16, 20, tzinfo=NY)
    assert next_at(friday, "16:15").date() == date(2026, 10, 26)  # Monday
    assert next_at(friday, "16:15", weekdays_only=False).date() == date(2026, 10, 24)
    assert next_at(friday, "16:25").date() == date(2026, 10, 23)


# ---------------------------------------------------------------------------- the broker (fast)


def test_sim_broker_fills_at_the_open_with_ibkr_costs(tmp_path):
    b = SimBroker(tmp_path / "b.json", cash=10_000)
    b.submit(client_order_id="A", ticker="X", side="BUY", qty=100, order_type="MARKET", tif="AT_THE_OPEN")
    fills, _ = b.open_session({"X": 50.0}, ts=1)
    assert [(f.qty, f.px, f.commission) for f in fills] == [(100, 50.01, 1.0)]  # one tick, $1 minimum
    assert b.cash == pytest.approx(10_000 - 100 * 50.01 - 1.0)
    # State outlives the process, like a real broker.
    again = SimBroker(tmp_path / "b.json")
    assert again.positions() == {"X": 100} and again.state["orders"]["A"]["status"] == "FILLED"
    # A duplicate client order ID is refused, a sale beyond the position is refused (no shorting).
    assert (
        again.submit(client_order_id="A", ticker="X", side="BUY", qty=1, order_type="MARKET", tif="AT_THE_OPEN")[
            "status"
        ]
        == "REJECTED"
    )
    assert (
        again.submit(client_order_id="B", ticker="X", side="SELL", qty=101, order_type="MARKET", tif="AT_THE_OPEN")[
            "reason"
        ]
        == "short sale not permitted in this account"
    )


def test_sim_broker_partial_fill_cancels_the_auction_remainder(tmp_path):
    b = SimBroker(tmp_path / "b.json", cash=10_000)
    b.inject("partial", fraction=0.5)
    b.submit(client_order_id="A", ticker="X", side="BUY", qty=10, order_type="MARKET", tif="AT_THE_OPEN")
    fills, cancelled = b.open_session({"X": 20.0}, ts=1)
    assert fills[0].qty == 5 and cancelled[0]["status"] == "CANCELED" and b.positions() == {"X": 5}


# ---------------------------------------------------------------------------- the live node


def test_live_node_matches_the_backtest_fill_for_fill(goog_lab, frames, tmp_path):
    """Same agents, same bars, same costs: the paper path must reproduce the backtest exactly."""
    days = 7
    h = _harness(goog_lab, frames, tmp_path)
    for _ in range(days):
        r = h.session()
        assert r.error is None, r.error
    live = [(f.side, f.qty, f.px, f.commission) for s in h.sessions for f in s.fills]

    goog = frames["GOOG"]
    bt = backtest.run(
        goog_lab,
        [backtest.Sleeve(TREND.make(), "GOOG", 1.0)],
        frames,
        goog.index[D0].date(),
        goog.index[D0 + days].date(),
        capital=goog_lab.paper.capital,
        target_vol=goog_lab.portfolio.target_vol,
    )
    expected = [(f.side, f.qty, f.price, f.commission) for f in bt.fills]
    assert len(live) >= 4  # an entry and three flips
    assert live == expected
    _assert_books_agree(h)
    assert "reconciliation_mismatch" not in _kinds(h)


def test_crash_between_order_and_fill_is_recovered_from_broker_reports(goog_lab, frames, tmp_path):
    h = _harness(goog_lab, frames, tmp_path)
    first = h.session(fill="offline")  # orders sent, node gone before the open, broker fills anyway
    assert first.fills and first.ledger["pending"]  # the order was journaled before it was sent
    second = h.session()
    assert second.error is None
    kinds = _kinds(h)
    assert "fill_recovered" in kinds and "reconciliation_mismatch" not in kinds
    _assert_books_agree(h)


def test_restart_with_an_unfilled_order_never_duplicates_it(goog_lab, frames, tmp_path):
    h = _harness(goog_lab, frames, tmp_path, spec=SignalSpec("buy_and_hold"))
    h.session(fill="none")  # accepted by the broker, not filled (no auction yet)
    h.session(fill="none")  # restarted: the order is still open at the broker
    orders = h.broker().state["orders"]
    assert len(orders) == 1, "a restart must not send the same order twice"
    h.session()  # the auction comes: one fill
    assert len(h.broker().state["orders"]) == 1
    assert h.broker().positions() == {"GOOG": h.ledger()["positions"]["GOOG"]}
    # Client order IDs embed the second they were made: never reused across restarts.
    assert not any(o.get("reason") == "duplicate order id" for o in orders.values())


def test_broker_reject_is_reported_and_the_order_retried_next_day(goog_lab, frames, tmp_path):
    h = _harness(goog_lab, frames, tmp_path, spec=SignalSpec("buy_and_hold"))
    h.session(before_start=lambda b: b.inject("reject", reason="no trading permissions"))
    assert "order_rejected" in _kinds(h) and h.broker().positions() == {}
    assert h.ledger()["pending"] == {}
    h.session()
    assert h.broker().positions()
    _assert_books_agree(h)


def test_partial_fill_is_booked_and_topped_up(goog_lab, frames, tmp_path):
    h = _harness(goog_lab, frames, tmp_path, spec=SignalSpec("buy_and_hold"))
    first = h.session(before_start=lambda b: b.inject("partial", fraction=0.5))
    assert len(first.fills) == 1
    half = first.fills[0].qty
    _assert_books_agree(h)
    h.session()
    assert h.broker().positions()["GOOG"] > half  # the rest bought the next day
    _assert_books_agree(h)


def test_a_non_paper_account_never_receives_an_order(goog_lab, frames, tmp_path):
    h = _harness(goog_lab, frames, tmp_path, spec=SignalSpec("buy_and_hold"))
    h.session(before_start=lambda b: b.inject("account", id="U7654321"))
    assert h.broker().state["orders"] == {}
    assert "paper_guard" in _kinds(h)


def test_kill_switch_stops_orders(goog_lab, frames, tmp_path):
    h = _harness(goog_lab, frames, tmp_path, spec=SignalSpec("buy_and_hold"))
    set_kill_switch(h.runtime / "KILL", "halt", "test")
    h.session()
    assert h.broker().state["orders"] == {}
    assert "kill_switch" in _kinds(h)
    heartbeat = json.loads((h.runtime / "heartbeat.json").read_text())
    assert heartbeat["kill_switch"] == "halt" and heartbeat["account"] == "IB-DU1234567"
