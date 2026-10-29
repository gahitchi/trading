"""Passive fills on recorded data: queue position, latency, cancels, and what the following moves take back."""

import json
import math

import numpy as np
import pandas as pd
import pytest
from nautilus_trader.model import (
    AggressorSide,
    BookAction,
    BookOrder,
    OrderBookDelta,
    OrderSide,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)

from lab.cli import main as lab_main
from lab.maker_study import MakerSettings, evaluate, markouts, simulate_fills
from tests.conftest import REPO
from tests.test_micro import IID, MS

_seq = iter(range(1, 10**9))


def snap(ts, bid=99.95, ask=100.05, bid_size=2.0, ask_size=2.0, tick=0.1):
    """A full five-level snapshot: touch sizes as given, 1.0 behind them."""
    out = [OrderBookDelta.clear(IID, next(_seq), ts, ts)]
    orders = [(OrderSide.BUY, round(bid - k * tick, 2), bid_size if k == 0 else 1.0) for k in range(5)]
    orders += [(OrderSide.SELL, round(ask + k * tick, 2), ask_size if k == 0 else 1.0) for k in range(5)]
    for k, (side, p, q) in enumerate(orders):
        order = BookOrder(side, Price(p, 2), Quantity(q, 3), 0)
        out.append(OrderBookDelta(IID, BookAction.ADD, order, 128 * (k == 9), next(_seq), ts, ts))
    return out


def trade(ts, price, size, seller=True):
    side = AggressorSide.SELL if seller else AggressorSide.BUY
    return TradeTick(IID, Price(price, 2), Quantity(size, 3), side, TradeId(f"t{next(_seq)}"), ts, ts)


def run(deltas, trades, tick=0.1, **kw):
    return simulate_fills(deltas, trades, MakerSettings(**kw), tick)


def test_a_bid_fills_only_after_the_queue_ahead_of_it_trades():
    deltas = snap(0)  # bid order placed at 99.95 behind 2.0, live at 50 ms
    fills, _ = run(deltas, [trade(60 * MS, 99.95, 1.5)])
    assert fills.empty
    fills, _ = run(deltas, [trade(60 * MS, 99.95, 1.5), trade(70 * MS, 99.95, 1.0)])
    assert list(fills[["ts", "side", "price"]].itertuples(index=False, name=None)) == [(70 * MS, 1, 99.95)]
    assert fills["mid"][0] == pytest.approx(100.0)


def test_no_fill_before_the_order_is_live_and_a_trade_through_fills_it():
    deltas = snap(0)
    assert run(deltas, [trade(10 * MS, 99.85, 5.0)])[0].empty  # 10 ms: still on its way
    fills, _ = run(deltas, [trade(60 * MS, 99.85, 0.1, seller=True), trade(60 * MS, 100.15, 0.1, seller=False)])
    assert sorted(fills["side"]) == [-1, 1]  # both sides traded through
    assert sorted(fills["price"]) == [99.95, 100.05]


def test_cancels_shrink_the_queue_and_a_better_bid_cancels_the_order():
    deltas = snap(0) + snap(55 * MS, bid_size=0.5)  # the level thins to 0.5: at most 0.5 can be ahead
    fills, _ = run(deltas, [trade(60 * MS, 99.95, 0.6)])
    assert len(fills) == 1
    deltas = snap(0) + snap(55 * MS, bid=100.0)  # a better bid: the order is no longer at the touch
    fills, _ = run(deltas, [trade(60 * MS, 99.95, 5.0)])  # would have filled it
    assert fills.empty


def test_markouts_are_signed_for_the_maker_and_net_of_the_fee():
    mids = (np.array([0, 1000, 2000]) * MS, np.array([100.0, 99.0, 101.0]))
    fills = pd.DataFrame(
        {"ts": [0, 0], "side": [1, -1], "price": [99.95, 100.05], "mid": [100.0, 100.0], "favour": [0.5, -0.5]}
    )
    m = markouts(fills, mids, 1.0)
    assert m[0] == pytest.approx((99 / 99.95 - 1) * 1e4)  # bought, then the mid fell: a loss
    assert m[1] == pytest.approx(-(99 / 100.05 - 1) * 1e4)  # sold, then the mid fell: a gain
    assert markouts(fills, mids, 3.0).isna().all()  # past the data
    table = evaluate(fills, mids, MakerSettings(horizons_s=(1.0,), maker_fee_bps=2.0)).set_index("group")
    assert table.loc["all", "fills"] == 2 and table.loc["favours", "fills"] == 1 and table.loc["neutral", "fills"] == 0
    assert table.loc["all", "at_fill_bps"] == pytest.approx(5.0, rel=1e-3)  # half of a 0.1 spread at 100
    assert table.loc["favours", "net_bps"] == pytest.approx(m[0] - 2.0)
    assert table.loc["all", "fills_per_h"] == pytest.approx(2 / (2 / 3600))


def test_markouts_do_not_use_stale_prices_or_cross_a_ten_minute_hole():
    mids = (np.array([0, 1, 2, 602, 603, 604]) * 1000 * MS, np.array([100, 101, 102, 200, 201, 202]))
    fills = pd.DataFrame({"ts": np.array([1, 2, 602]) * 1000 * MS, "side": 1, "price": [100, 101, 200]})
    one = markouts(fills, mids, 1.0)
    assert one.iloc[0] == pytest.approx(200.0)
    assert np.isnan(one.iloc[1])
    assert one.iloc[2] == pytest.approx(50.0)
    assert markouts(fills.iloc[:1], mids, 602.0).isna().all()


def test_resting_orders_do_not_survive_a_ten_minute_hole():
    restart = 600_000 * MS
    fills, _ = run(
        snap(0) + snap(restart + 100 * MS),
        [trade(restart, 99.95, 3.0), trade(restart + 160 * MS, 99.95, 3.0)],
    )
    assert fills["ts"].tolist() == [restart + 160 * MS]


def test_fill_rate_uses_recorded_time_instead_of_time_the_recorder_was_off():
    mids = (np.array([0, 1, 601, 602]) * 1000 * MS, np.array([100.0] * 4))
    fills = pd.DataFrame({"ts": np.array([0, 601]) * 1000 * MS, "side": 1, "price": 99.95, "mid": 100.0, "favour": 0.0})
    table = evaluate(fills, mids, MakerSettings(horizons_s=(1.0,))).set_index("group")
    assert table.loc["all", "fills"] == 2
    assert table.loc["all", "fills_per_h"] == pytest.approx(3600.0)  # two fills in two recorded seconds


def test_adverse_selection_shows_in_the_markout():
    """Every 200 ms a seller sweeps the bid and the market drops a tick: fills at the bid lose afterwards."""
    deltas, trades, bid = [], [], 100.0
    for i in range(300):
        ts = i * 200 * MS
        deltas += snap(ts, bid=round(bid - 0.05, 2), ask=round(bid + 0.05, 2))
        trades.append(trade(ts + 100 * MS, round(bid - 0.05, 2), 10.0))
        bid = round(bid - 0.1, 2)
    fills, mids = run(deltas, trades)
    table = evaluate(fills, mids, MakerSettings(horizons_s=(1.0,))).set_index("group")
    assert table.loc["all", "fills"] > 250
    assert table.loc["all", "at_fill_bps"] > 0 > table.loc["all", "markout_bps"]
    assert table.loc["all", "net_bps"] < table.loc["all", "markout_bps"]
    assert math.isfinite(table.loc["all", "t"])


def test_maker_study_command_respects_the_holdout_and_logs_the_trial(tmp_path, capsys):
    from tests.test_fast_research import catalog_with

    deltas = [d for i in range(50) for d in snap(i * 100 * MS)]
    path = catalog_with(tmp_path / "catalog", deltas)
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    study = ["--config", str(tmp_path / "lab.toml"), "maker-study", "BTCUSDT", "--catalog", path]
    assert lab_main(study) == 0
    assert "study settings tried: 1" in capsys.readouterr().out
    assert lab_main([*study, "--improve", "1"]) == 0  # the tick size comes from the recorded instrument
    assert "study settings tried: 2" in capsys.readouterr().out
    trial = json.loads((tmp_path / "results" / "fast_trials.jsonl").read_text().splitlines()[-1])
    assert trial["kind"] == "maker-study" and trial["window"][1] == deltas[-1].ts_init
    with pytest.raises(SystemExit, match="holdout"):
        lab_main([*study, "--end", "2100-01-01"])


def test_the_spread_captured_is_measured_while_the_order_was_still_at_the_touch():
    """The book can show the bid level gone before the trades that emptied it arrive: the fill then prints with
    the mid already below the bid. The half spread captured is against the last mid with the order at the touch."""
    deltas = snap(0) + snap(55 * MS, bid=99.85, ask=99.95)  # the book drops a tick before the prints arrive
    fills, mids = run(deltas + snap(100 * MS, bid=99.85, ask=99.95), [trade(60 * MS, 99.95, 3.0)])
    assert list(fills["price"]) == [99.95]
    assert fills["mid"][0] == pytest.approx(100.0)  # not 99.90, the mid after the level had gone
    table = evaluate(fills, mids, MakerSettings(horizons_s=(0.0,))).set_index("group")
    assert table.loc["all", "at_fill_bps"] > 0 > table.loc["all", "markout_bps"]


def test_stepping_inside_a_wide_spread_takes_the_front_of_a_new_queue():
    wide = snap(0, bid=99.95, ask=100.25)  # three ticks wide
    fills, _ = run(wide, [trade(60 * MS, 100.05, 0.1), trade(60 * MS, 100.15, 0.1, seller=False)], improve_ticks=1)
    assert sorted(fills["price"]) == [100.05, 100.15]  # one tick inside on each side, filled by the first print
    assert fills["mid"].tolist() == [100.1, 100.1]
    narrow = snap(0)  # one tick wide: no room inside, so the order joins the touch and its queue
    fills, _ = run(narrow, [trade(60 * MS, 99.95, 1.0)], improve_ticks=1, tick=0.1)
    assert fills.empty
    deltas = wide + snap(55 * MS, bid=100.05, ask=100.25)  # someone else joins at its price: behind it
    fills, _ = run(deltas, [trade(60 * MS, 100.05, 0.1)], improve_ticks=1)
    assert list(fills["price"]) == [100.05]
    deltas = wide + snap(55 * MS, bid=100.15, ask=100.25)  # someone steps ahead of it: cancel
    assert run(deltas, [trade(60 * MS, 100.05, 5.0)], improve_ticks=1)[0].empty
    with pytest.raises(ValueError, match="tick"):
        simulate_fills(wide, [], MakerSettings(improve_ticks=1))
