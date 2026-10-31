"""Crash research: archive files in (no network in tests), crash trades after costs out, with no look-ahead, a
time holdout and every configuration counted."""

import io
import json
import zipfile
from dataclasses import replace
from datetime import date
from urllib.error import HTTPError

import numpy as np
import pandas as pd
import pytest

from lab import crash_data as cd
from lab import crash_study as cs
from lab import fast_research as fr
from lab.cli import main as lab_main
from tests.conftest import REPO

T0 = pd.Timestamp("2024-01-01", tz="UTC")
M = 60_000


def zipped(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    return buf.getvalue()


def kline_csv(open_ms: list[int], header: bool) -> str:
    rows = [f"{t},1,2,0.5,1.5,10,{t + M - 1},100,3,4,40,0" for t in open_ms]
    head = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
    return (head if header else "") + "\n".join(rows) + "\n"


# --- fetching -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("header", [True, False])
def test_klines_parse_with_or_without_header_and_are_stamped_at_the_minute_end(header):
    t = int(T0.value // 1_000_000)
    df = cd.parse_klines(zipped(kline_csv([t, t + M], header)))
    assert list(df["ts"]) == [T0 + pd.Timedelta(minutes=1), T0 + pd.Timedelta(minutes=2)]
    assert df.iloc[0][list(cd.KLINE_COLUMNS)].tolist() == [1, 2, 0.5, 1.5, 100, 40]


def test_microsecond_timestamps_are_read_as_milliseconds():
    t = int(T0.value // 1_000)
    df = cd.parse_klines(zipped(kline_csv([t], True).replace(f",{t + M - 1},", ",0,")))
    assert df["ts"].iloc[0] == T0 + pd.Timedelta(minutes=1)


def test_metrics_parse_open_interest_value():
    text = (
        "create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
        "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        "2024-01-01 00:05:00,BTCUSDT,100,4200000,1,1,1,1\n"
    )
    df = cd.parse_metrics(zipped(text))
    assert df["ts"].iloc[0] == T0 + pd.Timedelta(minutes=5) and df["oi_value"].iloc[0] == 4.2e6


def test_periods_are_months_then_the_days_of_this_month():
    assert cd.periods("klines", date(2024, 11, 15), date(2025, 1, 3)) == [
        "2024-11",
        "2024-12",
        "2025-01-01",
        "2025-01-02",
    ]
    assert cd.periods("oi", date(2024, 12, 30), date(2025, 1, 2)) == ["2024-12-30", "2024-12-31", "2025-01-01"]


def not_found(url):
    return HTTPError(url, 404, "Not Found", {}, None)


def test_update_resumes_remembers_old_gaps_and_falls_back_to_days(tmp_path):
    seen = []

    def get(url):
        seen.append(url)
        if url.endswith(("-2024-01.zip", "-2024-02.zip")):  # before the listing; last month not published yet
            raise not_found(url)
        return zipped(kline_csv([int(T0.value // 1_000_000)], True))

    added, _ = cd.update(tmp_path, "klines", "X", date(2024, 1, 1), date(2024, 3, 2), get=get)
    assert added == 29 + 1  # 2024-02 has 29 days, standing in for the month, then 2024-03-01
    assert (tmp_path / "klines" / "X" / "2024-01.missing").exists()  # old: not asked again
    assert not (tmp_path / "klines" / "X" / "2024-02.missing").exists()  # recent: asked again next time
    seen.clear()
    assert cd.update(tmp_path, "klines", "X", date(2024, 1, 1), date(2024, 3, 2), get=get)[0] == 0
    assert all("2024-01" not in u for u in seen) and len(seen) == 1  # only the month, which is still missing
    assert cd.last_cached(tmp_path) == pd.Timestamp("2024-03-02", tz="UTC")


def test_other_http_errors_are_not_taken_for_missing_data(tmp_path):
    def get(url):
        raise HTTPError(url, 403, "Forbidden", {}, None)

    with pytest.raises(HTTPError):
        cd.update(tmp_path, "oi", "X", date(2024, 1, 1), date(2024, 1, 3), get=get)


# --- the crash detector ---------------------------------------------------------------------------------------


def market(n_days=10, crash_at=None, drop=0.08, oi_move=-0.05, seed=0):
    """1-minute bars of a quiet market, optionally with a crash: `drop` over 30 minutes from minute crash_at, on
    10x volume, open interest moving by oi_move, then a rebound of half the drop over the next 60 minutes."""
    rng = np.random.default_rng(seed)
    n = n_days * 1440
    r = rng.normal(0, 0.0004, n)
    vol = np.full(n, 1e5)
    oi = np.full(n, 1e9)
    if crash_at is not None:
        r[crash_at : crash_at + 30] = np.log(1 - drop) / 30
        r[crash_at + 30 : crash_at + 90] = np.log(1 / (1 - drop / 2)) / 60
        vol[crash_at : crash_at + 30] = 1e6
        oi[crash_at + 15 :] = 1e9 * (1 + oi_move)
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[100, close[:-1]]
    ts = T0 + pd.to_timedelta(np.arange(1, n + 1), unit="min")
    k = pd.DataFrame(
        {
            "ts": ts,
            "open": open_,
            "high": np.maximum(open_, close) * 1.0001,
            "low": np.minimum(open_, close) * 0.9999,
            "close": close,
            "quote_volume": vol,
            "taker_buy_quote": vol / 2,
        }
    )
    o = pd.DataFrame({"ts": ts[4::5], "oi_value": oi[4::5]})  # every 5 minutes from 00:05
    return k, o


WICK = cs.CrashSettings(mode="wick", drop_pct=5, window_min=60, vol_mult=3, oi_pct=1, tp_pct=2, sl_pct=10, hold_min=240)


def test_open_interest_is_used_five_minutes_after_its_snapshot_and_not_when_stale():
    k, o = market(n_days=1)
    o = o.copy()
    o.loc[o.index[10], "oi_value"] = 2e9  # taken at 00:55
    b = cs.bars(k, o)
    assert b.loc[T0 + pd.Timedelta(minutes=59), "oi"] == 1e9
    assert b.loc[T0 + pd.Timedelta(minutes=60), "oi"] == 2e9
    gappy = o[(o["ts"] < T0 + pd.Timedelta(hours=2)) | (o["ts"] > T0 + pd.Timedelta(hours=3))]
    b = cs.bars(k, gappy)
    assert np.isnan(b.loc[T0 + pd.Timedelta(hours=2, minutes=30), "oi"])


def test_a_liquidation_flush_signals_a_wick_and_a_rising_oi_drop_signals_a_breakdown():
    at = 3 * 1440 + 600
    b = cs.bars(*market(crash_at=at))
    sig = cs.signals(b, WICK)
    assert sig.any() and at < np.flatnonzero(sig)[0] < at + 31
    assert sig.sum() < 100  # only around the crash
    breakdown = cs.CrashSettings(mode="breakdown", drop_pct=5, window_min=60, vol_mult=3, oi_pct=0)
    assert not cs.signals(b, breakdown).any()  # open interest fell: not a breakdown
    b_up = cs.bars(*market(crash_at=at, oi_move=+0.05))
    assert cs.signals(b_up, breakdown).any() and not cs.signals(b_up, WICK).any()


def test_signals_never_look_ahead():
    at = 3 * 1440 + 600
    k, o = market(crash_at=at)
    full = cs.signals(cs.bars(k, o), WICK)
    cut = at + 20
    k2, o2 = k.iloc[:cut], o[o["ts"] < k["ts"].iloc[cut - 1]]
    assert (cs.signals(cs.bars(k2, o2), WICK) == full[:cut]).all()


def test_no_crash_no_trade_and_the_quiet_market_has_no_edge():
    b = cs.bars(*market())
    assert not cs.signals(b, WICK).any()
    assert cs.trades(b, WICK).empty


# --- trades ---------------------------------------------------------------------------------------------------


def frame(o, h, lo, c):
    idx = T0 + pd.to_timedelta(np.arange(1, len(o) + 1), unit="min")
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c}, index=idx, dtype=float)


S = cs.CrashSettings(mode="wick", tp_pct=2, sl_pct=3, hold_min=3, delay_min=0, vol_target=0, window_min=1)


@pytest.mark.parametrize(
    ("bars_", "expect"),
    [
        (([100, 100, 101], [100, 102.5, 101], [99, 99.5, 100], [100, 101, 101]), (1, 0.02, "target")),
        (([100, 100, 98], [100, 100.5, 98], [99, 96.5, 97], [100, 99, 97]), (1, -0.03, "stop")),
        (([100, 100, 95], [100, 100.5, 96], [99, 99.5, 94], [100, 100, 95]), (2, -0.05, "stop")),  # gaps through
        (([100, 100, 100], [100, 103, 100], [99, 96, 100], [100, 100, 100]), (1, -0.03, "stop")),  # both: stop
        (([100, 100, 100, 100], [100, 101] * 2, [99, 99.5] * 2, [100, 100.5, 100.5, 101]), (3, 0.01, "time")),
    ],
)
def test_exits(bars_, expect):
    b = frame(*bars_)
    got = cs._exit(*(b[x].to_numpy() for x in ("open", "high", "low", "close")), 1, S)
    assert got[0] == expect[0] and got[1] == pytest.approx(expect[1]) and got[2] == expect[2]


def test_a_short_mirrors_the_long():
    b = frame([100, 100, 99], [100, 101, 99], [99, 97.5, 98], [100, 99, 98])
    short = cs.CrashSettings(mode="breakdown", tp_pct=2, sl_pct=3, hold_min=3, delay_min=0, window_min=1)
    k, gross, why = cs._exit(*(b[x].to_numpy() for x in ("open", "high", "low", "close")), 1, short)
    assert (k, why) == (1, "target") and gross == pytest.approx(0.02)


def test_one_crash_is_one_trade_and_costs_are_paid():
    b = frame([100] * 10, [101] * 10, [99.5] * 10, [100] * 10)
    sig = np.ones(10, dtype=bool)
    t = cs.trades(b, replace(S, window_min=4), sig)
    # signal 0 -> entry 1, time exit at bar 3; then 4 quiet minutes: next signal 7 -> entry 8, exit at bar 9
    assert list(t["exit"]) == [b.index[3], b.index[9]]
    assert (t["net"] == t["gross"] - S.cost).all() and S.cost == pytest.approx(0.002)


def test_trades_are_sized_to_the_volatility_target():
    k, o = market(crash_at=3 * 1440 + 600)
    b = cs.bars(k, o)
    w = cs.weights(b, cs.CrashSettings(vol_target=0.5))
    sigma = 0.0004 * np.sqrt(cs.MINUTES_PER_YEAR)  # the quiet market's annualized vol, about 29%
    assert w[2 * 1440] == pytest.approx(min(0.5 / sigma, cs.MAX_WEIGHT), rel=0.05)
    assert w[100] == 0  # under a day of history: no trade
    assert (cs.weights(b, cs.CrashSettings(vol_target=0)) == 1).all()


def test_the_wick_trade_earns_the_rebound_and_the_daily_t_counts_days_not_trades():
    at = 3 * 1440 + 600
    b = cs.bars(*market(crash_at=at))
    t = cs.trades(b, WICK)
    assert len(t) == 1 and t["reason"].iloc[0] == "target" and t["net"].iloc[0] > 0
    st = cs.summary({"A": t, "B": t}, b.index[0], b.index[-1])
    assert st["trades"] == 2 and st["trade_days"] == 1 and st["days"] == 11


# --- holdout and the command line ------------------------------------------------------------------------------


def cache(root, symbol, k, o):
    for kind, df in (("klines", k), ("oi", o)):
        d = root / kind / symbol
        d.mkdir(parents=True, exist_ok=True)
        for day, part in df.groupby(df["ts"].dt.floor("D")):
            part.to_parquet(d / f"{day:%Y-%m-%d}.parquet", index=False)


def test_research_never_sees_the_holdout(tmp_path):
    root = tmp_path / "crash"
    cache(root, "AUSDT", *market(n_days=30, crash_at=25 * 1440 + 600))  # the crash is in the last days
    b = cs.boundary(tmp_path / "results", root)
    assert b == (cd.last_cached(root) - pd.Timedelta(days=cs.HOLDOUT_DAYS)).floor("D")
    late = T0 + pd.Timedelta(days=20)
    research = cs.run(root, ["AUSDT"], WICK, late)
    assert research.trades["AUSDT"].empty and research.end < late
    held = cs.run(root, ["AUSDT"], WICK, late, holdout=True)
    assert len(held.trades["AUSDT"]) == 1 and held.start >= late


def test_cli_research_counts_trials_and_meets_the_holdout_once(tmp_path, capsys):
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    root = tmp_path / "data" / "crash"
    for i, sym in enumerate(("AUSDT", "BUSDT")):
        cache(root, sym, *market(n_days=200, crash_at=(5 + 10 * i) * 1440 + 600, seed=i))
    run = ["--config", str(tmp_path / "lab.toml"), "crash", "study", "--symbols", "AUSDT", "BUSDT", "XUSDT"]

    assert lab_main([*run, "--drop-pct", "5", "--tp-pct", "2"]) == 0
    out = capsys.readouterr().out
    assert "configurations tried: 1" in out and "no data, left out: XUSDT" in out and "baseline" in out
    assert "2 trades" in out
    assert lab_main([*run, "--drop-pct", "6", "--tp-pct", "2"]) == 0
    assert "configurations tried: 2" in capsys.readouterr().out
    assert lab_main([*run, "--mode", "breakdown"]) == 0
    assert "configurations tried: 1" in capsys.readouterr().out  # counted per mode
    assert lab_main([*run, "--holdout"]) == 0
    assert "HOLDOUT test" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="--again"):
        lab_main([*run, "--holdout"])
    trials = [json.loads(x) for x in (tmp_path / "results" / fr.TRIALS).read_text().splitlines()]
    assert [t["holdout"] for t in trials] == [False, False, False, True]
    assert all(t["kind"] == "crash-study" and "sr_daily" in t["result"] for t in trials)
    assert trials[0]["params"]["drop_pct"] == 5 and trials[2]["params"]["hold_min"] == 1440  # the preset
