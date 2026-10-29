"""Execution evidence ends with the observed feed; disconnects and shutdown do not invent fills."""

import json

import pytest
from nautilus_trader.model import BookAction, OrderBookDelta, TradeTick

from lab import fast_research as fr
from lab.cli import main as lab_main
from lab.fast import backtest
from lab.safety import set_kill_switch
from tests.conftest import REPO
from tests.test_maker import IID, MS, catalog, cfg, ranging_market


def shifted(deltas, trades, offset):
    return (
        [
            OrderBookDelta(
                d.instrument_id, d.action, d.order, d.flags, d.sequence, d.ts_event + offset, d.ts_init + offset
            )
            for d in deltas
        ],
        [
            TradeTick(
                t.instrument_id, t.price, t.size, t.aggressor_side, t.trade_id, t.ts_event + offset, t.ts_init + offset
            )
            for t in trades
        ],
    )


def gappy_market():
    a, b = ranging_market(steps=5, sweep=0.01)
    c, d = shifted(*ranging_market(steps=5, sweep=0.01), 600_000 * MS)
    return a + c, b + d


def test_resting_orders_cannot_silently_cross_a_recording_hole(tmp_path):
    path = catalog(tmp_path / "catalog", *gappy_market())
    with pytest.raises(ValueError, match=r"recording gap.*outstanding orders"):
        backtest(path, cfg(), latency_ms=0)


def test_inventory_cannot_be_freely_closed_or_erased_at_a_hole(tmp_path):
    # No outstanding quote is needed: the inventory alone makes the interval unknowable.
    a, b = ranging_market(steps=5, sides="s", bid_size=5, ask_size=0.5)
    c, d = shifted(*ranging_market(steps=5), 600_000 * MS)
    path = catalog(tmp_path / "catalog", a + c, b + d)
    with pytest.raises(ValueError, match=r"recording gap.*open inventory"):
        backtest(path, cfg(min_favour=0.1, max_hold_s=1000), latency_ms=0)


def test_a_flat_inactive_strategy_can_resume_after_a_snapshot(tmp_path):
    path = catalog(tmp_path / "catalog", *gappy_market())
    kill = tmp_path / "KILL"
    set_kill_switch(kill, "halt", "flat gap test")
    r = backtest(path, cfg(), latency_ms=0, kill_file=str(kill))
    assert r["pnl"] == 0 and r["fills"] == 0
    assert r["recording_segments"] == 2
    assert r["terminal"]["net_qty"] == r["terminal"]["outstanding_orders"] == 0


def test_cash_and_trades_continue_across_a_flat_disconnect(tmp_path):
    a, b = ranging_market(steps=5)
    c, d = shifted(*ranging_market(steps=5), 600_000 * MS)
    path = catalog(tmp_path / "catalog", a + c, b + d)
    r = backtest(path, cfg(), latency_ms=0, maker_bps=0)
    assert r["recording_segments"] == 2
    assert r["entries"] == r["fills"] == 20 and r["round_trips"] == 10
    assert r["pnl"] == r["cash_pnl"] == pytest.approx(0.01)
    assert r["terminal"]["net_qty"] == r["terminal"]["outstanding_orders"] == 0


def test_inflight_submissions_block_gap_resumption(tmp_path):
    path = catalog(tmp_path / "catalog", *gappy_market())
    with pytest.raises(ValueError, match=r"recording gap.*outstanding orders"):
        backtest(path, cfg(), latency_ms=1000)


def test_resumption_requires_a_fresh_book_snapshot(tmp_path):
    deltas, trades = gappy_market()
    deltas = [d for d in deltas if not (d.ts_init >= 600_000 * MS and d.action == BookAction.CLEAR)]
    path = catalog(tmp_path / "catalog", deltas, trades)
    with pytest.raises(ValueError, match=r"recording gap.*snapshot"):
        backtest(path, cfg(), latency_ms=0)


def test_trades_without_a_fresh_book_are_rejected(tmp_path):
    deltas, trades = ranging_market(steps=5)
    _, late = shifted(*ranging_market(steps=1), 2000 * MS)
    path = catalog(tmp_path / "catalog", deltas, trades + late)
    with pytest.raises(ValueError, match=r"trade.*fresh book"):
        backtest(path, cfg(), latency_ms=0)


@pytest.mark.parametrize("damage", ["incomplete", "one-sided", "crossed"])
def test_damaged_complete_books_cannot_supply_execution_or_terminal_valuation(tmp_path, damage):
    deltas, trades = ranging_market(steps=1)
    if damage == "incomplete":
        deltas.pop()
    elif damage == "one-sided":
        deltas = [d for d in deltas if d.action == BookAction.CLEAR or d.order.side.name == "BUY"]
        last = deltas[-1]
        deltas[-1] = OrderBookDelta(
            last.instrument_id, last.action, last.order, 128, last.sequence, last.ts_event, last.ts_init
        )
    else:
        deltas, trades = ranging_market(steps=1, spread_ticks=0)
    path = catalog(tmp_path / "catalog", deltas, trades)
    with pytest.raises(ValueError, match=r"incomplete book|unusable complete book"):
        backtest(path, cfg(), latency_ms=0)


def test_end_of_recording_keeps_inventory_and_marks_its_loss(tmp_path):
    report = tmp_path / "report.json"
    # Accumulate a long position, then mark down; no observable liquidation beyond the final event.
    path = catalog(tmp_path / "catalog", *ranging_market(steps=20, sides="s", drift_every=1))
    r = backtest(path, cfg(report=str(report), max_hold_s=1000), latency_ms=150, maker_bps=0, taker_bps=0)
    assert r["terminal"]["net_qty"] > 0
    assert r["unrealized_pnl"] < 0
    assert r["pnl"] == pytest.approx(r["cash_pnl"] + r["unrealized_pnl"])
    assert r["fills"] == r["entries"]  # shutdown did not sell into the last cached bid
    assert r["return_stats"]["daily_returns"]["1970-01-01"] == pytest.approx(r["pnl"] / 10_000)
    rep = json.loads(report.read_text())
    assert rep["pnl"] == pytest.approx(r["pnl"])
    assert rep["position"]["net"] == r["terminal"]["net_qty"]
    assert rep["running"] is False


@pytest.mark.parametrize("cancel_ms", [50, 150, 300])
def test_a_quote_can_fill_while_its_cancellation_is_in_transit(tmp_path, cancel_ms):
    deltas, trades = ranging_market(steps=3, spread_ticks=5, sweep=1, sides="s", drift_every=1)
    trades = [t for t in trades if t.ts_init >= 100 * MS]
    path = catalog(tmp_path / "catalog", deltas, trades)
    prompt = backtest(path, cfg(), latency_ms=0, cancel_latency_ms=0, maker_bps=0)
    delayed = backtest(path, cfg(), latency_ms=0, cancel_latency_ms=cancel_ms, maker_bps=0)
    assert prompt["entries"] == 0
    assert delayed["entries"] == 1  # the old bid is traded through at 140 ms, before its cancel arrives
    assert delayed["terminal"]["net_qty"] == pytest.approx(0.01)


@pytest.mark.parametrize("latency_ms", [50, 150, 300])
def test_submit_in_transit_at_the_last_tick_is_reported_without_a_future_fill(tmp_path, latency_ms):
    path = catalog(tmp_path / "catalog", *ranging_market(steps=1))
    r = backtest(path, cfg(), latency_ms=latency_ms, maker_bps=0)
    assert r["end_ns"] == 60 * MS
    assert r["terminal"]["outstanding_orders"] > 0
    assert r["round_trips"] == 0
    if latency_ms >= 150:
        assert r["entries"] == r["fills"] == 0  # no submit has reached the venue by the cutoff


@pytest.mark.parametrize("kw", [{"latency_ms": -1}, {"latency_ms": float("nan")}, {"cancel_latency_ms": -1}])
def test_invalid_latency_is_refused(tmp_path, kw):
    path = catalog(tmp_path / "catalog", *ranging_market(steps=1))
    with pytest.raises(ValueError, match=r"latency.*finite.*nonnegative"):
        backtest(path, cfg(), **kw)


def test_cli_logs_execution_assumptions_and_terminal_exposure(tmp_path, capsys):
    path = catalog(tmp_path / "catalog", *ranging_market(steps=10, sides="s"))
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    args = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path]
    args += ["--strategy", "maker", "--size", "0.01", "--latency-ms", "50", "--cancel-latency-ms", "300"]
    assert lab_main(args) == 0
    out = capsys.readouterr().out
    assert "cancel 300" in out and "marked" in out and "outstanding orders" in out
    trial = json.loads((tmp_path / "results" / "fast_trials.jsonl").read_text().splitlines()[-1])
    assert trial["params"]["cancel_latency_ms"] == 300
    assert trial["params"]["max_book_gap_ms"] == 1000
    assert trial["params"]["execution_model"] == "observed-flat-gap-v1"
    assert trial["result"]["terminal"]["net_qty"] > 0


def test_rejected_research_replay_remains_a_counted_attempt(tmp_path):
    path = catalog(tmp_path / "catalog", *gappy_market())
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    args = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path]
    args += ["--strategy", "maker", "--size", "0.01", "--latency-ms", "0"]
    with pytest.raises(SystemExit, match=r"recording gap.*outstanding orders"):
        lab_main(args)
    trials = tmp_path / "results" / "fast_trials.jsonl"
    assert trials.exists(), "an unavailable replay must not disappear from the configuration history"
    trial = json.loads(trials.read_text())
    assert trial["result"]["status"] == "unavailable"
    assert "outstanding orders" in trial["result"]["error"]
    assert "pnl" not in trial["result"]
    assert trial["holdout"] is False


def test_rejected_holdout_replay_is_consumed_and_logged(tmp_path):
    path = catalog(tmp_path / "catalog", *gappy_market())
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    results = tmp_path / "results"
    fr.atomic_json(results / fr.PARTITION, {"version": 1, "instruments": {IID: {"holdout_start": 0}}})
    args = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path]
    args += ["--strategy", "maker", "--size", "0.01", "--latency-ms", "0", "--holdout"]
    with pytest.raises(SystemExit, match=r"recording gap.*outstanding orders"):
        lab_main(args)
    state = fr.read_json(results / fr.HOLDOUT_STATE)
    assert state["attempts"][0]["status"] == "failed"
    trial = json.loads((results / fr.TRIALS).read_text())
    assert trial["holdout"] is True and trial["result"]["status"] == "unavailable"
    with pytest.raises(SystemExit, match="--again"):
        lab_main(args)
    assert len((results / fr.TRIALS).read_text().splitlines()) == 1
