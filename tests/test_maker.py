"""PassiveMaker: post-only quotes at the touch, inventory limits, stale-inventory exits, and the risk manager."""

import json

import pytest
from nautilus_trader.model import (
    AggressorSide,
    BookAction,
    BookOrder,
    CryptoPerpetual,
    Currency,
    InstrumentId,
    OrderBookDelta,
    OrderSide,
    Price,
    Quantity,
    Symbol,
    TradeId,
    TradeTick,
)
from nautilus_trader.persistence import ParquetDataCatalog

from lab.cli import main as lab_main
from lab.fast import backtest, make_strategy
from lab.maker import PassiveMaker, PassiveMakerConfig
from lab.risk_manager import RiskManagedStrategy
from lab.safety import set_kill_switch

IID = "BTCUSDT-PERP.BINANCE"
MS = 1_000_000
USDT = Currency.from_str("USDT")


def ranging_market(
    steps=600, bid=999.9, spread_ticks=1, sweep=5.0, drift_every=0, sides="sb", bid_size=2.0, ask_size=2.0
):
    """A book of 10 levels a side refreshed every 100 ms; between refreshes a seller hits the bid and a buyer lifts
    the ask (sides "s", "b" or "sb") with `sweep` each. drift_every: every n steps the whole book drops a tick."""
    iid = InstrumentId.from_str(IID)
    deltas, trades, seq = [], [], 0
    for i in range(steps):
        if drift_every and i and i % drift_every == 0:
            bid = round(bid - 0.1, 1)
        ask = round(bid + 0.1 * spread_ticks, 1)
        ts = i * 100 * MS
        seq += 1
        deltas.append(OrderBookDelta.clear(iid, seq, ts, ts))
        levels = [(OrderSide.BUY, round(bid - k * 0.1, 1)) for k in range(10)]
        levels += [(OrderSide.SELL, round(ask + k * 0.1, 1)) for k in range(10)]
        for k, (side, px) in enumerate(levels):
            seq += 1
            size = (bid_size if side == OrderSide.BUY else ask_size) + 0.001 * (i % 2)  # a refresh is a change
            order = BookOrder(side, Price(px, 1), Quantity(size, 3), 0)
            deltas.append(OrderBookDelta(iid, BookAction.ADD, order, 128 if k == 19 else 0, seq, ts, ts))
        for j, (side, px) in enumerate(((AggressorSide.SELL, bid), (AggressorSide.BUY, ask))):
            if "sb"[j] not in sides:
                continue
            t = ts + (40 + 20 * j) * MS
            trades.append(TradeTick(iid, Price(px, 1), Quantity(sweep, 3), side, TradeId(f"t{i}-{j}"), t, t))
    return deltas, trades


def catalog(path, deltas, trades):
    path.mkdir()
    c = ParquetDataCatalog(str(path))
    inst = CryptoPerpetual(
        InstrumentId.from_str(IID), Symbol("BTCUSDT"), Currency.from_str("BTC"), USDT, USDT, False, 1, 3,
        Price.from_str("0.1"), Quantity.from_str("0.001"), 0, 0,
    )  # fmt: skip
    c.write_instruments([inst])
    c.write_order_book_deltas(deltas)
    c.write_trade_ticks(trades)
    return str(path)


def cfg(**kw):
    return PassiveMakerConfig(**{"instrument_id": IID, "quote_size": 0.01, **kw})


def run(tmp_path, market=None, maker_bps=0.0, config=None, **kw):
    path = tmp_path / f"catalog{len(list(tmp_path.glob('catalog*')))}"
    catalog(path, *(market or ranging_market()))
    return backtest(str(path), cfg(**(config or {})), maker_bps=maker_bps, taker_bps=5, latency_ms=0, **kw)


def test_a_ranging_market_pays_the_maker_the_spread(tmp_path):
    r = run(tmp_path)
    assert r["stopped"] is None and r["risk_denials"] == {}
    assert r["fills"] == r["entries"] == 1200  # both quotes fill every 100 ms
    assert r["round_trips"] == 600 and r["mean_trip"] == pytest.approx(0.1 * 0.01)  # one tick on 0.01 each
    assert r["pnl"] == pytest.approx(0.6)
    fees = run(tmp_path, maker_bps=2.0)
    assert fees["commissions"] == pytest.approx(1200 * 0.01 * 1000 * 0.0002, rel=0.01)
    assert fees["pnl"] == pytest.approx(0.6 - fees["commissions"], abs=0.01)  # 1 bp of spread < 2 x 2 bp of fees


def test_quotes_wait_their_turn_in_the_queue(tmp_path):
    r = run(tmp_path, ranging_market(sweep=1.0))  # 1.0 traded per side every 100 ms, 2.0 resting ahead
    assert r["fills"] == 400  # each quote fills on the third print at its price, not the first


def test_inventory_stops_at_its_cap_until_the_hold_limit_exits_it(tmp_path):
    sellers_only = ranging_market(sides="s")
    r = run(tmp_path, sellers_only, config={"max_inventory": 0.03, "max_hold_s": 1_000})
    assert r["fills"] == 3  # 0.03 long, then the bid is no longer quoted
    held = run(tmp_path, sellers_only, config={"max_inventory": 0.03, "max_hold_s": 5})
    assert held["fills"] > 6 and held["commissions"] > 0  # exited at the touch (taker) every 5 s, then again


def test_stepping_inside_a_wide_spread(tmp_path):
    wide = ranging_market(spread_ticks=3)
    joined = run(tmp_path, wide)
    assert joined["mean_trip"] == pytest.approx(0.3 * 0.01)  # at the touches, three ticks apart
    inside = run(tmp_path, wide, config={"improve_ticks": 1})
    assert inside["fills"] == 1200
    assert inside["mean_trip"] == pytest.approx(0.1 * 0.01)  # one tick inside each: one tick apart, first in line


def test_a_side_the_book_leans_against_is_not_quoted(tmp_path):
    heavy_offer = ranging_market(bid_size=0.5, ask_size=5.0, sweep=6.0)  # imbalance about -0.8
    r = run(tmp_path, heavy_offer, config={"min_favour": 0.0, "max_hold_s": 1_000})
    assert r["fills"] == 3  # only the ask is quoted: short to the 0.03 cap, then nothing
    assert run(tmp_path, heavy_offer, config={"max_hold_s": 1_000})["fills"] == 1200


def test_the_risk_manager_stops_the_maker(tmp_path):
    kill = tmp_path / "KILL"
    set_kill_switch(kill, "halt", "operator test")
    halted = run(tmp_path, kill_file=str(kill))
    assert halted["fills"] == 0 and halted["risk_denials"].get("halted", 0) > 0
    falling = ranging_market(drift_every=5, sides="s")  # sellers only, the book drops a tick every 0.5 s
    lost = run(tmp_path, falling, max_loss=0.02, config={"max_hold_s": 1_000})
    assert lost["stopped"].startswith("daily loss")
    assert lost["pnl"] > -0.1  # stopped near the limit, not at the end


def test_the_maker_goes_through_risk():
    from lab.fast import fast_risk

    assert isinstance(make_strategy(cfg(), fast_risk("BINANCE", capital=1e4, max_loss=10)), PassiveMaker)
    assert issubclass(PassiveMaker, RiskManagedStrategy)
    with pytest.raises(ValueError):
        cfg(max_inventory=0.005)  # below one quote
    with pytest.raises(ValueError):
        cfg(min_favour=2.0)


def test_the_report_names_the_maker_and_its_settings(tmp_path):
    report = tmp_path / "paper.json"
    r = run(tmp_path, config={"report": str(report), "improve_ticks": 1})
    rep = json.loads(report.read_text())
    assert rep["strategy"] == "PassiveMaker" and rep["params"]["improve_ticks"] == 1
    assert rep["fills"] == r["fills"] and rep["pnl"] == pytest.approx(r["pnl"])


def test_fast_backtest_runs_the_maker_on_research_data(tmp_path, capsys):
    from tests.conftest import REPO

    path = catalog(tmp_path / "catalog", *ranging_market())
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    run = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path]
    run += ["--strategy", "maker", "--size", "0.01", "--maker-bps", "0", "--latency-ms", "0"]
    assert lab_main(run) == 0
    out = capsys.readouterr().out
    assert "passive maker" in out and "round trips" in out
    trial = json.loads((tmp_path / "results" / "fast_trials.jsonl").read_text().splitlines()[-1])
    assert trial["params"]["strategy"] == "passive_maker" and trial["params"]["max_inventory"] is None
