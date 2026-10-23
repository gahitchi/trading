"""Quick trades through the real agents (C003): timing with delayed data, flat at close, financing by night,
per-day caps, and the profile rule."""

import re
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from lab import backtest, intraday
from lab.config import load_config
from lab.margin import DAY
from lab.signals.base import Signal
from lab.signals.builtin import BUILTIN

NY = ZoneInfo("America/New_York")
DAYS = pd.bdate_range("2026-03-02", periods=6)


def frames(minutes=15, days=DAYS):
    idx = [t for d in days for t in intraday.session_bars(d, minutes)]
    o = 100 + 0.1 * np.arange(len(idx))  # every bar opens at its own price: a fill names its bar
    return pd.DataFrame(
        {"open": o, "high": o + 0.2, "low": o - 0.2, "close": o + 0.05, "volume": 1e6}, index=pd.DatetimeIndex(idx)
    )


def lab(goog_lab, profile="aggressive", **trading):
    path = goog_lab.root / "lab.toml"
    path.write_text(re.sub(r'(?m)^profile = "\w+"', f'profile = "{profile}"', path.read_text()))
    cfg = load_config(path)
    return replace(cfg, trading=replace(cfg.trading, **trading))


class Long(Signal):
    name = "test_long"

    def target(self, w):
        return 1.0


class Short(Signal):
    name = "test_short"

    def target(self, w):
        return -1.0


class Flip(Signal):
    name = "test_flip"

    def target(self, w):
        return 1.0 if len(w) and int(w.minute[-1]) // 15 % 2 else -1.0


def run(cfg, signal, df, speed="15m", **kw):
    return backtest.run(
        cfg, [backtest.Sleeve(signal, "GOOG", 1.0)], {"GOOG": df}, DAYS[0].date(), None, speed=speed, **kw
    )


def ny(ts):
    return datetime.fromtimestamp(ts / 1e9, NY)


def bar_open(df, at):
    return float(df.loc[pd.Timestamp(at), "open"])


def test_delayed_data_fills_at_the_opening_print_of_the_bar_when_the_data_arrives(goog_lab):
    cfg = lab(goog_lab, data_delay_min=15)
    df = frames()
    r = run(cfg, Long(), df)
    first = r.fills[0]
    # The 09:30-09:45 bar is known at 10:00 (15 min delay); the order fills at the 10:00 bar's opening print.
    assert ny(first.ts).strftime("%H:%M:%S") == "10:00:05" and first.side == "BUY"
    assert first.price == pytest.approx(bar_open(df, "2026-03-02 10:00"), abs=0.011)  # one tick of slippage
    # Real-time data: the same bar is acted on at 09:45.
    r0 = run(lab(goog_lab, data_delay_min=0), Long(), df)
    assert ny(r0.fills[0].ts).strftime("%H:%M:%S") == "09:45:05"


def test_flat_at_close_exits_in_the_last_interval_and_re_enters_next_session(goog_lab):
    cfg = lab(goog_lab, data_delay_min=15)
    r = run(cfg, Long(), frames())
    by_day = {}
    for f in r.fills:
        by_day.setdefault(ny(f.ts).date(), []).append(f)
    assert len(by_day) == len(DAYS)
    for fills in by_day.values():
        assert [f.side for f in fills] == ["BUY", "SELL"]
        assert ny(fills[-1].ts).strftime("%H:%M") == "15:45"  # the session's last bar fills the exit
        assert sum(f.qty if f.side == "BUY" else -f.qty for f in fills) == 0  # flat every night
    assert "flat_at_close" in {e.kind for e in r.risk_events}
    assert r.financing == 0 and r.speed == "15m" and r.sessions == len(DAYS)
    assert "intraday_max_drawdown" in r.metrics


def test_overnight_shorts_pay_borrow_per_night_and_flat_books_pay_nothing(goog_lab):
    df = frames()
    held = run(lab(goog_lab, flat_at_close=False), Short(), df)
    assert held.fills[0].side == "SELL" and "flat_at_close" not in {e.kind for e in held.risk_events}
    pos = 0.0
    for f in held.fills:
        pos += f.qty if f.side == "BUY" else -f.qty
        assert pos < 0  # resized as the price moves, never closed: the short is held through every night
    nights = (DAYS[-1] - DAYS[0]).days  # calendar nights, the weekend included
    short_value = held.fills[0].qty * held.fills[0].price
    assert held.borrow_fees == pytest.approx(short_value * goog_lab.margin.borrow_apr * nights * DAY, rel=0.3)
    flat = run(lab(goog_lab), Short(), df)
    assert flat.borrow_fees == 0 and flat.financing == 0
    assert all(f.side in ("SELL", "BUY") for f in flat.fills) and len(flat.fills) == 2 * len(DAYS)


def test_caps_count_every_rebalance_of_the_session(goog_lab):
    r = run(lab(goog_lab), Flip(), frames(), execution_overrides={"max_orders_per_day": 3})
    per_day = {}
    for f in r.fills:
        per_day[ny(f.ts).date()] = per_day.get(ny(f.ts).date(), 0) + 1
    assert max(per_day.values()) <= 4  # three orders, plus the uncapped exit at the close
    assert "daily_cap" in {e.kind for e in r.risk_events}


@pytest.mark.parametrize("speed, minutes, first", [("1h", 60, "11:30:05"), ("5m", 5, "09:50:05")])
def test_other_speeds_round_the_delay_up_to_whole_bars(goog_lab, speed, minutes, first):
    r = run(lab(goog_lab, data_delay_min=15), Long(), frames(minutes), speed=speed)
    assert ny(r.fills[0].ts).strftime("%H:%M:%S") == first
    assert all(ny(f.ts).time() < datetime.strptime("16:00", "%H:%M").time() for f in r.fills)


@pytest.mark.parametrize("profile", ["balanced", "cautious", "calm"])
def test_quick_speed_is_refused_for_slow_profiles(goog_lab, profile):
    with pytest.raises(ValueError, match="dynamic or aggressive"):
        run(lab(goog_lab, profile), Long(), frames())


def test_intraday_techniques_need_a_quick_speed(goog_lab, goog):
    with pytest.raises(ValueError, match="quick speed"):
        backtest.run(
            lab(goog_lab),
            [backtest.Sleeve(BUILTIN["orb"](), "GOOG", 1.0)],
            {"GOOG": goog},
            date(2010, 1, 4),
            date(2010, 3, 1),
        )


@pytest.mark.parametrize("name", ["orb", "intraday_momentum", "vwap_reversion"])
def test_intraday_techniques_run_through_the_agents(goog_lab, name):
    rng = np.random.default_rng(7)
    df = frames(5, pd.bdate_range("2026-03-02", periods=8))
    steps = rng.normal(0, 0.002, len(df)).cumsum()
    o = 100 * np.exp(steps)
    df = df.assign(open=o, close=o * (1 + rng.normal(0, 0.001, len(df))))
    df = df.assign(high=df[["open", "close"]].max(axis=1) * 1.001, low=df[["open", "close"]].min(axis=1) * 0.999)
    r = run(lab(goog_lab, data_delay_min=0), BUILTIN[name](), df, speed="5m")
    assert r.fills and r.signal_errors == 0 and r.ledger_vs_account == pytest.approx(0, abs=0.05)
    assert all(ny(f.ts).time() < datetime.strptime("16:00", "%H:%M").time() for f in r.fills)


# ----------------------------------------------------------------------------- research commands


def _store(cfg, sessions=10, minutes=15):
    sp = intraday.speed(f"{minutes}m" if minutes != 60 else "1h")
    rng = np.random.default_rng(3)
    df = frames(minutes, pd.bdate_range("2026-03-02", periods=sessions))
    o = 100 * np.exp(rng.normal(0, 0.002, len(df)).cumsum())
    df = df.assign(open=o, close=o * (1 + rng.normal(0, 0.001, len(df))))
    df = df.assign(high=df[["open", "close"]].max(axis=1) * 1.001, low=df[["open", "close"]].min(axis=1) * 0.999)
    intraday.save(cfg, "GOOG", df, sp, source="test", adjusted=True)
    return intraday.load_many(cfg, ["GOOG"], sp)


def test_intraday_holdout_is_the_last_sessions_and_is_guarded(goog_lab):
    from lab import experiments
    from lab.config import Period

    cfg = lab(goog_lab)
    fr = _store(cfg)
    research, holdout = experiments.intraday_split(cfg, fr)
    assert (research.start, research.end, holdout.start) == (date(2026, 3, 2), date(2026, 3, 11), date(2026, 3, 12))
    with pytest.raises(experiments.HoldoutUsed, match="intraday holdout"):
        experiments.compare(cfg, [], ["GOOG"], Period("custom", date(2026, 3, 2), None), fr, speed="15m")
    comp, results = experiments.compare(cfg, [], ["GOOG"], research, fr, speed="15m", register=False)
    assert comp.data_quality["speed"] == "15m" and "sessions of history" in comp.data_quality["intraday_history"]
    assert results[0].label == "buy_and_hold (control)"
    assert "flat_at_close" not in {e.kind for e in results[0].risk_events}  # the control holds overnight


def test_cli_backtest_and_holdout_at_a_quick_speed(goog_lab, capsys):
    import json

    from lab.cli import main

    cfg = lab(goog_lab)
    _store(cfg)
    args = ["--config", str(cfg.root / "lab.toml")]
    assert main([*args, "backtest", "orb", "vwap_reversion", "--speed", "15m"]) in (0, None)
    out = capsys.readouterr().out
    assert "sessions of history" in out and "flat at every close" in out
    assert main([*args, "holdout", "orb", "--speed", "15m"]) in (0, None)
    ledger = json.loads((cfg.results_dir / "holdout_ledger.json").read_text())
    assert ledger[-1]["speed"] == "15m" and ledger[-1]["period"][0] == "2026-03-12"
    with pytest.raises(SystemExit, match="already used the holdout"):
        main([*args, "holdout", "orb", "--speed", "15m"])
    # A slow profile cannot run a quick speed from the command line either.
    lab(goog_lab, "balanced")
    with pytest.raises(SystemExit, match="quick speed"):
        main([*args, "backtest", "--speed", "15m"])


def test_intraday_fetches_accumulate_history_and_rebase_adjusted_prices(goog_lab):
    sp = intraday.speed("15m")
    first = frames(15, pd.bdate_range("2026-03-02", periods=4))
    intraday.save(goog_lab, "GOOG", first, sp, source="test", adjusted=True)
    # The next fetch overlaps two sessions and comes after a 2:1 split: every adjusted price halved.
    later = frames(15, pd.bdate_range("2026-03-04", periods=4))
    later = later.assign(
        **{k: first.reindex(later.index)[k].fillna(later[k]) / 2 for k in ("open", "high", "low", "close")}
    )
    e = intraday.save(goog_lab, "GOOG", later, sp, source="test", adjusted=True)
    assert e["sessions"] == 6 and e["rebased_history_by"] == pytest.approx(0.5)
    back = intraday.load(goog_lab, "GOOG", sp)
    assert back.loc["2026-03-02 09:30", "open"] == pytest.approx(first.iloc[0]["open"] / 2)
