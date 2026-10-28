"""Funding carry: public history in (no network in tests), P&L after costs out, with no look-ahead, a time
holdout and every configuration counted."""

import json
import math

import numpy as np
import pandas as pd
import pytest

from lab import carry_data as cd
from lab import carry_study as cs
from lab import fast_research as fr
from lab.cli import main as lab_main
from tests.conftest import REPO

H = 3_600_000
T0 = pd.Timestamp("2024-01-01", tz="UTC")


# --- fetching -------------------------------------------------------------------------------------------------


def test_binance_pages_forward_and_skips_the_open_kline():
    now = int(T0.value // 1_000_000) + 5 * H + 1  # the kline that opened at 5 h is still open
    seen = []

    def get(url):
        seen.append(url)
        start = int(url.split("startTime=")[1].split("&")[0])
        return [[t, "0", "0", "0", str(t // H), "0"] for t in range(start, now, H)][:2]

    t0 = int(T0.value // 1_000_000)
    klines = cd._binance_closes(f"{cd.BINANCE_FAPI}/klines?symbol=X", 2, t0, now, get, lambda: None)
    assert [t for t, _ in klines] == [t0 + k * H for k in range(1, 6)]  # stamped at the hour's end; 6 h is open
    assert klines[0][1] == t0 // H
    assert len(seen) == 3 and all("limit=2" in u for u in seen)  # pages of 2 until a short page


def test_bybit_pages_backwards_until_the_start():
    t0 = int(T0.value // 1_000_000)
    now = t0 + 450 * 8 * H
    rows = [{"fundingRateTimestamp": str(t0 + k * 8 * H), "fundingRate": str(k * 1e-6)} for k in range(450)]

    def get(url):
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        page = [r for r in rows if int(q["startTime"]) <= int(r["fundingRateTimestamp"]) <= int(q["endTime"])]
        return {"result": {"list": sorted(page, key=lambda r: -int(r["fundingRateTimestamp"]))[: int(q["limit"])]}}

    got = cd._bybit_funding("X", t0, now, get, lambda: None)
    assert sorted(t for t, _ in got) == [t0 + k * 8 * H for k in range(450)]  # three pages of at most 200


def test_update_appends_only_what_is_new(tmp_path):
    calls = []

    def fake(venue, market, kind, symbol, start_ms, now_ms, **_):
        calls.append(start_ms)
        return [(t, float(t // H)) for t in range(start_ms - start_ms % H + H, now_ms + 1, H)]

    cd_download, cd.download = cd.download, fake
    try:
        assert cd.update(tmp_path, "binance", "perp", "close", "X", T0, T0 + pd.Timedelta(hours=10)) == (10, 10)
        assert cd.update(tmp_path, "binance", "perp", "close", "X", T0, T0 + pd.Timedelta(hours=15)) == (5, 15)
    finally:
        cd.download = cd_download
    s = cd.load(tmp_path, "binance", "perp", "close", "X")
    assert s.index.is_unique and s.index[-1] == T0 + pd.Timedelta(hours=15)
    assert calls[1] == int(T0.value // 1_000_000) + 10 * H + 1  # resumes after the last cached row


# --- the backtest ---------------------------------------------------------------------------------------------


def write(root, venue, market, kind, symbol, s: pd.Series):
    p = cd.path_of(root, venue, market, kind, symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ts": s.index, "value": s.to_numpy()}).to_parquet(p, index=False)


def market(root, symbol="BTCUSDT", days=60, rate=1e-4, bybit_rate=0.0, seed=0, start=T0):
    """Hourly spot and perpetual closes (the perpetual at a small noisy premium), funding every 8 h."""
    rng = np.random.default_rng(seed)
    hours = pd.date_range(start + pd.Timedelta(hours=1), periods=days * 24, freq="h")
    spot = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.003, len(hours)))), index=hours)
    perp = spot * (1 + rng.normal(0, 1e-4, len(hours)))
    settle = pd.date_range(start, periods=days * 3, freq="8h")
    write(root, "binance", "spot", "close", symbol, spot)
    write(root, "binance", "perp", "close", symbol, perp)
    write(root, "binance", "perp", "funding", symbol, pd.Series(rate, index=settle))
    write(root, "bybit", "perp", "close", symbol, perp * (1 + rng.normal(0, 1e-4, len(hours))))
    write(root, "bybit", "perp", "funding", symbol, pd.Series(bybit_rate, index=settle))


def flat_legs(hours=24 * 10, rate=1e-4, short_jump=None):
    idx = pd.date_range(T0 + pd.Timedelta(hours=1), periods=hours, freq="h")
    long = pd.Series(100.0, index=idx)
    short = long.copy()
    if short_jump is not None:
        short[short.index >= short_jump] = 101.0
    return cs.Legs(long, short, pd.Series(rate, index=pd.date_range(T0, periods=hours // 8 + 1, freq="8h")))


def test_cash_carry_earns_funding_after_entry_and_pays_both_legs_costs():
    s = cs.CarrySettings(lookback_h=24, entry_apr=5.0)  # 1 bp every 8 h is 10.95% a year
    h = cs.hourly(flat_legs())
    r = cs.backtest(h, s)
    entry = r.index[(r["w"] != 0).argmax()]
    assert entry == T0 + pd.Timedelta(hours=24)  # first 8 h decision with a full 24 h of history (from 1:00)
    later = r[r.index > entry]
    settlements = (later.index.hour % 8 == 0).sum()
    assert r["funding"].sum() == pytest.approx(settlements * 1e-4)  # entry hour's settlement went to the seller
    assert r["cost"].sum() == pytest.approx((5 + 10 + 2) / 1e4)  # one entry: perp + spot taker + slippage x 2
    assert r["net"].sum() == pytest.approx(r["funding"].sum() - r["cost"].sum())


def test_the_short_leg_rallying_against_the_spot_leg_is_a_basis_loss():
    jump = T0 + pd.Timedelta(days=5)
    r = cs.backtest(cs.hourly(flat_legs(short_jump=jump)), cs.CarrySettings(lookback_h=24, entry_apr=5.0))
    assert r.loc[jump, "basis"] == pytest.approx(-0.01)
    assert r["basis"].sum() == pytest.approx(-0.01)


def test_negative_funding_is_not_traded_in_cash_but_flips_the_cross_trade():
    h = cs.hourly(flat_legs(rate=-1e-4))
    assert (cs.positions(h, cs.CarrySettings(lookback_h=24, entry_apr=5.0)) == 0).all()
    cross = cs.CarrySettings(mode="cross", lookback_h=24, entry_apr=5.0)
    r = cs.backtest(h, cross)
    assert set(r["w"].unique()) == {0.0, -1.0}
    assert r["funding"].sum() > 0  # short the leg that receives: long the side paying less
    assert cross.cost == pytest.approx((5 + 5 + 2) / 1e4)


def test_hysteresis_holds_between_exit_and_entry_and_closes_below_exit():
    hours = 24 * 12
    lg = flat_legs(hours)
    rate = pd.Series(2e-4, index=lg.funding.index)  # 21.9% a year
    rate[rate.index >= T0 + pd.Timedelta(days=4)] = 0.5e-4  # 5.5%: between exit (0) and entry (10)
    rate[rate.index >= T0 + pd.Timedelta(days=8)] = -0.5e-4
    w = cs.positions(cs.hourly(cs.Legs(lg.long, lg.short, rate)), cs.CarrySettings(lookback_h=24, entry_apr=10.0))
    assert w[T0 + pd.Timedelta(days=6)] == 1  # still in at 5.5%
    assert w.iloc[-1] == 0  # out once the trailing rate is negative
    assert (w.diff().abs() > 0).sum() == 2  # one entry, one exit


def test_a_decision_never_sees_later_funding_or_prices():
    s = cs.CarrySettings(lookback_h=24, entry_apr=5.0)
    lg = flat_legs(24 * 10)
    base = cs.positions(cs.hourly(lg), s)
    cut = T0 + pd.Timedelta(days=5)
    shocked = cs.Legs(
        lg.long.where(lg.long.index <= cut, 50.0),
        lg.short,
        lg.funding.where(lg.funding.index <= cut, -0.01),
    )
    after = cs.positions(cs.hourly(shocked), s)
    pd.testing.assert_series_equal(base[base.index <= cut], after[after.index <= cut])
    assert (after[after.index > cut + pd.Timedelta(hours=8)] == 0).all()  # it does react once the data arrives


# --- statistics -----------------------------------------------------------------------------------------------


def test_statistics():
    x = np.random.default_rng(1).normal(0.001, 0.01, 500)
    assert cs.newey_west_t(x, lags=0) == pytest.approx(x.mean() / (x.std() / math.sqrt(len(x))))
    ar = np.repeat(x[:100], 5)  # strongly autocorrelated: NW shrinks the t-statistic
    assert abs(cs.newey_west_t(ar)) < abs(cs.newey_west_t(ar, lags=0))
    assert cs.probabilistic_sharpe(0.1, 400, 0.0, 3.0, sr0=0.1) == pytest.approx(0.5)
    assert cs.probabilistic_sharpe(0.1, 400, 0.0, 3.0) > 0.97
    few, many = [0.0, 0.05, -0.05], list(np.linspace(-0.05, 0.05, 50))
    assert cs.expected_max_sharpe([0.1]) == 0.0
    assert 0 < cs.expected_max_sharpe(few) < cs.expected_max_sharpe(many)


# --- holdout and CLI ------------------------------------------------------------------------------------------


def test_research_never_reads_the_holdout_and_the_holdout_starts_flat(tmp_path):
    root, results = tmp_path / "carry", tmp_path / "results"
    market(root, days=400)
    b = cs.boundary(results, root)
    last = T0 + pd.Timedelta(days=400)
    assert b == (last - pd.Timedelta(days=cs.HOLDOUT_DAYS)).floor("D")
    s = cs.CarrySettings(lookback_h=24, entry_apr=5.0)
    research, _ = cs.run(root, ["BTCUSDT"], s, b)
    assert research["BTCUSDT"].index[-1] < b
    market(root, days=400, rate=-0.01, seed=7)  # rewrite everything: research before b must not change
    write(root, "binance", "perp", "funding", "BTCUSDT", pd.Series(1e-4, index=pd.date_range(T0, b, freq="8h")))
    again, _ = cs.run(root, ["BTCUSDT"], s, b)
    assert again["BTCUSDT"]["funding"].sum() == pytest.approx(research["BTCUSDT"]["funding"].sum())
    market(root, days=400)
    hold, _ = cs.run(root, ["BTCUSDT"], s, b, holdout=True)
    first = hold["BTCUSDT"].iloc[0]
    assert hold["BTCUSDT"].index[0] == b and first["funding"] == 0 and first["cost"] == pytest.approx(s.cost)
    assert cs.boundary(results, root) == b  # reserved once


def test_release_moves_the_boundary_later_only(tmp_path):
    root, results = tmp_path / "carry", tmp_path / "results"
    market(root, days=300)
    b = cs.boundary(results, root)
    with pytest.raises(ValueError, match="reason"):
        cs.release(results, root, " ")
    old, new = cs.release(results, root, "strategy frozen, holdout spent")
    assert old == b and new == T0 + pd.Timedelta(days=300, hours=1)
    with pytest.raises(fr.HoldoutGuard, match="nothing has been fetched"):
        cs.release(results, root, "again")
    part = json.loads((results / cs.PARTITION).read_text())
    assert [e["reason"] for e in part["history"]] == ["first carry study", "strategy frozen, holdout spent"]


def test_cli_research_counts_trials_and_meets_the_holdout_once(tmp_path, capsys):
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    root = tmp_path / "data" / "carry"
    market(root, "BTCUSDT", days=400, rate=2e-4)
    market(root, "ETHUSDT", days=400, rate=1e-4, bybit_rate=3e-4, seed=1)
    run = ["--config", str(tmp_path / "lab.toml"), "carry", "study", "--symbols", "BTCUSDT", "ETHUSDT", "XYZUSDT"]

    assert lab_main(run) == 0
    out = capsys.readouterr().out
    assert "configurations tried: 1" in out and "no data, left out: XYZUSDT" in out
    assert "always in the trade" in out and "BTCUSDT" in out
    assert lab_main([*run, "--entry-apr", "20"]) == 0
    assert "configurations tried: 2" in capsys.readouterr().out
    assert lab_main([*run, "--mode", "cross"]) == 0
    assert "configurations tried: 1" in capsys.readouterr().out  # counted per trade
    assert lab_main([*run, "--holdout"]) == 0
    assert "HOLDOUT test" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="--again"):
        lab_main([*run, "--holdout"])
    assert lab_main([*run, "--holdout", "--again", "reading the report"]) == 0
    trials = [json.loads(x) for x in (tmp_path / "results" / fr.TRIALS).read_text().splitlines()]
    assert [t["holdout"] for t in trials] == [False, False, False, True, True]
    assert all(t["kind"] == "carry-study" and "sr_daily" in t["result"] for t in trials)
