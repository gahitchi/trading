"""Trading speeds and intraday bars (C003: Gabriel asked for 1h, 15m and 5m, quick trades for dynamic and
aggressive only)."""

import re
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from lab import intraday
from lab.config import ConfigError, load_config
from lab.data import DataError


def _bars(day="2026-03-02", minutes=15, sessions=1, price=100.0):
    idx = []
    for d in pd.bdate_range(day, periods=sessions):
        idx += intraday.session_bars(d, minutes)
    n = len(idx)
    close = price + np.arange(n) * 0.01
    return pd.DataFrame(
        {"open": close, "high": close + 0.05, "low": close - 0.05, "close": close, "volume": 1000.0},
        index=pd.DatetimeIndex(idx),
    )


def test_speeds_and_bar_specs():
    assert [s.bar_spec() for s in intraday.SPEEDS.values()] == ["1-DAY", "1-HOUR", "15-MINUTE", "5-MINUTE"]
    assert intraday.speed("1h").bars_per_session == 7  # the last hourly bar is 15:30-16:00
    assert intraday.speed("5m").bars_per_session == 78
    with pytest.raises(ValueError, match="unknown speed"):
        intraday.speed("1m")


@pytest.mark.parametrize("profile", ["calm", "cautious", "balanced", "custom"])
def test_quick_speeds_need_dynamic_or_aggressive(profile):
    intraday.check_speed_allowed("daily", profile)
    for s in ("1h", "15m", "5m"):
        with pytest.raises(ValueError, match="dynamic or aggressive"):
            intraday.check_speed_allowed(s, profile)


@pytest.mark.parametrize("profile", ["dynamic", "aggressive"])
def test_quick_speeds_allowed_for_fast_profiles(profile):
    for s in intraday.SPEEDS:
        intraday.check_speed_allowed(s, profile)


def _toml(cfg, profile="custom", **trading):
    path = cfg.root / "lab.toml"
    text = re.sub(r'(?m)^profile = "\w+"', f'profile = "{profile}"', path.read_text())
    for k, v in trading.items():
        line = next(ln for ln in text.splitlines() if ln.startswith(f"{k} =")) if f"\n{k} =" in text else None
        text = text.replace(line, f"{k} = {v}") if line else text.replace("\n[trading]\n", f"\n[trading]\n{k} = {v}\n")
    path.write_text(text)
    return path


def test_config_refuses_a_quick_speed_on_a_slow_profile(goog_lab):
    with pytest.raises(ConfigError, match="quick speed"):
        load_config(_toml(goog_lab, "balanced", speed="'5m'"))
    cfg = load_config(_toml(goog_lab, "aggressive", speed="'5m'"))
    assert cfg.trading.speed == "5m" and cfg.risk.max_short_gross > 0


@pytest.mark.parametrize(
    "key, value, msg",
    [
        ("turbo", "true", "turbo"),
        ("data_delay_min", "-1", "data_delay_min"),
        ("limit_collar", "0.5", "limit_collar"),
        ("intraday_holdout", "1.0", "intraday_holdout"),
        ("speed", "'1m'", "unknown speed"),
    ],
)
def test_config_validates_trading_keys(goog_lab, key, value, msg):
    original = (goog_lab.root / "lab.toml").read_text()
    try:
        with pytest.raises(ConfigError, match=msg):
            load_config(_toml(goog_lab, **{key: value}))
    finally:
        (goog_lab.root / "lab.toml").write_text(original)


def test_bar_end_is_cut_at_the_close():
    t = pd.Timestamp("2026-03-02 15:30")
    assert intraday.bar_end(t, 60) == pd.Timestamp("2026-03-02 16:00")
    assert intraday.bar_end(pd.Timestamp("2026-03-02 09:30"), 60) == pd.Timestamp("2026-03-02 10:30")
    w = intraday.in_closing_window
    assert w(pd.Timestamp("2026-03-02 15:45"), 15)  # the 15:45-16:00 bar is left to fill the exit
    assert not w(pd.Timestamp("2026-03-02 15:30"), 15)
    assert w(pd.Timestamp("2026-03-02 15:30"), 15, n=2)
    assert w(pd.Timestamp("2026-03-02 15:30"), 60)  # 1h: the last decision with a bar left is at 15:30
    assert not w(pd.Timestamp("2026-03-02 14:30"), 60)


def test_validate_accepts_a_regular_session():
    sp = intraday.speed("15m")
    out = intraday.validate_intraday(_bars(sessions=2), "X", sp)
    assert len(out) == 2 * 26 and out.index[0] == pd.Timestamp("2026-03-02 09:30")


@pytest.mark.parametrize(
    "stamp, msg",
    [
        ("2026-03-02 08:00", "outside regular hours"),
        ("2026-03-02 16:00", "outside regular hours"),
        ("2026-03-02 09:37", "grid"),
    ],
)
def test_validate_refuses_extended_hours_and_off_grid_bars(stamp, msg):
    df = _bars()
    row = df.iloc[[0]].copy()
    row.index = pd.DatetimeIndex([pd.Timestamp(stamp)])
    with pytest.raises(DataError, match=msg):
        intraday.validate_intraday(pd.concat([df, row]).sort_index(), "X", intraday.speed("15m"))


def test_validate_refuses_non_finite_prices():
    df = _bars()
    df.iloc[3, df.columns.get_loc("close")] = np.inf
    with pytest.raises(DataError):
        intraday.validate_intraday(df, "X", intraday.speed("15m"))


def test_save_and_load_round_trip_records_sessions(goog_lab):
    sp = intraday.speed("5m")
    df = _bars(minutes=5, sessions=3)
    df.iloc[10, df.columns.get_loc("close")] = np.nan  # an incomplete bar: dropped at import, recorded
    e = intraday.save(goog_lab, "abc", df, sp, source="test", adjusted=True)
    assert e["sessions"] == 3 and e["rows"] == 3 * 78 - 1 and len(e["dropped_incomplete"]) == 1
    back = intraday.load(goog_lab, "ABC", sp)
    assert len(back) == e["rows"] and back.index[0].hour == 9
    assert intraday.manifest(goog_lab, sp)["ABC"]["sessions"] == 3
    assert "3 sessions" in intraday.history_note({"ABC": back}, sp)
    with pytest.raises(DataError, match="no 1h data"):
        intraday.load(goog_lab, "ABC", intraday.speed("1h"))


def test_cli_imports_intraday_csv_with_utc_offsets(goog_lab, tmp_path):
    from lab.cli import main

    df = _bars(minutes=60)
    utc = df.index.tz_localize("America/New_York").tz_convert("UTC")
    csv = tmp_path / "bars.csv"
    df.set_axis(utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")).rename_axis("timestamp").to_csv(csv)
    rc = main(
        [
            "--config",
            str(goog_lab.root / "lab.toml"),
            "data",
            "import",
            str(csv),
            "--ticker",
            "ABC",
            "--speed",
            "1h",
            "--adjusted",
        ]
    )
    assert rc in (0, None)
    back = intraday.load(goog_lab, "ABC", intraday.speed("1h"))
    assert back.index[0] == pd.Timestamp("2026-03-02 09:30") and len(back) == 7


def test_default_config_is_balanced_and_daily():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "lab.toml")
    assert cfg.risk.profile == "balanced" and cfg.trading.speed == "daily" and cfg.risk.max_short_gross == 0
    fast = replace(cfg.trading, speed="1h")
    assert fast.speed == "1h"


# ----------------------------------------------------------------------------- intraday techniques


def _window(closes, minutes=15, prev_close=None, volume=None):
    """One session of bars closing every `minutes` from 09:30+minutes, optionally after a previous session."""
    from lab.signals.base import Window

    c = np.asarray(closes, dtype=float)
    minute = minutes * np.arange(1, len(c) + 1, dtype=float)
    dates = np.full(len(c), np.datetime64("2026-03-03"))
    if prev_close is not None:
        c = np.concatenate([[prev_close], c])
        minute = np.concatenate([[390.0], minute])
        dates = np.concatenate([[np.datetime64("2026-03-02")], dates])
    v = np.ones(len(c)) if volume is None else np.asarray(volume, dtype=float)
    return Window(c, c + 0.1, c - 0.1, c, v, dates, minute)


def _feed(sig, closes, **kw):
    out = []
    for i in range(1, len(closes) + 1):
        t = sig.target(_window(closes[:i], **kw))
        sig.last = t
        out.append(t)
    return out


def test_orb_breaks_out_stops_at_the_midpoint_and_does_not_retry():
    from lab.signals.builtin import BUILTIN

    # Opening range (first 30 min = two 15m bars): 100..101 (+-0.1). Breakout above, then a fall below the mid.
    targets = _feed(BUILTIN["orb"](), [100, 101, 100.8, 101.5, 101.2, 100.3, 101.6])
    assert targets[:3] == [0, 0, 0]  # forming, then inside the range
    assert targets[3:5] == [1, 1]  # closed above the range high: long, held above the midpoint
    assert targets[5:] == [0, 0]  # stopped at the midpoint; no second trade today


def test_orb_short_side_and_relative_volume_filter():
    from lab.signals.builtin import BUILTIN

    assert _feed(BUILTIN["orb"](), [100, 101, 99.5])[-1] == -1
    assert _feed(BUILTIN["orb"](short=0), [100, 101, 99.5])[-1] == 0
    # 'Stocks in play' only: no earlier session in the window to compare with -> no trade.
    assert _feed(BUILTIN["orb"](min_rvol=2), [100, 101, 101.5])[-1] == 0


def test_intraday_momentum_trades_the_last_half_hour_in_the_first_half_hours_direction():
    from lab.signals.builtin import BUILTIN

    sig = BUILTIN["intraday_momentum"]()
    # +1% by 10:00 against the previous close of 100; bar i closes at 15*(i+1) minutes, 15:30 is bar 23.
    targets = _feed(sig, [101.0] * 25, prev_close=100.0)
    assert set(targets[:23]) == {0}  # before 15:30
    assert targets[23:] == [1, 1]  # 15:30 onward: long
    down = _feed(BUILTIN["intraday_momentum"](), [99.0] * 25, prev_close=100.0)
    assert down[-1] == -1


def test_vwap_reversion_fades_a_stretch_and_exits_at_vwap():
    from lab.signals.builtin import BUILTIN

    calm = [100 + 0.01 * ((-1) ** i) for i in range(6)]  # 90 minutes of quiet trading around 100
    targets = _feed(BUILTIN["vwap_reversion"](), [*calm, 98.0, 99.0, 100.5])
    assert targets[:6] == [0] * 6
    assert targets[6] == 1  # far below VWAP: long
    assert targets[7] == 1  # still below
    assert targets[8] == 0  # back above VWAP: out


# ----------------------------------------------------------------------------- the live session clock


def test_session_grid_phase_and_fetch_times():
    from datetime import datetime

    from lab.agents.schedule import NY, Session

    s = Session(15, delay_min=15)
    day = datetime(2026, 3, 2, 12, 0, tzinfo=NY)
    ends = s.bar_ends(day)
    assert len(ends) == 26 and ends[0].strftime("%H:%M") == "09:45" and ends[-1].strftime("%H:%M") == "16:00"
    assert [s.phase(day.replace(hour=h, minute=m)) for h, m in ((9, 0), (10, 0), (15, 46), (16, 0))] == [
        "closed",
        "open",
        "closing",
        "closed",
    ]
    assert s.minute_of(day.replace(hour=10, minute=0)) == 30
    first = s.next_fetch(day.replace(hour=9, minute=0))
    assert first.strftime("%H:%M:%S") == "10:00:20"  # the 09:30-09:45 bar, 15 minutes late, plus the lag
    friday_late = datetime(2026, 3, 6, 17, 0, tzinfo=NY)
    assert s.next_fetch(friday_late).date().isoformat() == "2026-03-09"  # Monday
    h = Session(60, delay_min=15)
    assert [e.strftime("%H:%M") for e in h.bar_ends(day)][-2:] == ["15:30", "16:00"]


def test_pacing_refuses_too_many_live_requests(goog_lab):
    from lab import paper
    from lab.signals.registry import SignalSpec

    cfg = replace(goog_lab, trading=replace(goog_lab.trading, speed="5m"))
    paper.check_pacing(cfg, [SignalSpec("orb")], ["T"] * 20)  # 40 requests / 10 min: fine
    with pytest.raises(ValueError, match="pacing"):
        paper.check_pacing(cfg, [SignalSpec("orb"), SignalSpec("vwap_reversion")], ["T"] * 20)


def test_paper_node_refuses_intraday_techniques_at_the_daily_speed(goog_lab):
    from lab import paper
    from lab.signals.registry import SignalSpec

    with pytest.raises(ValueError, match="intraday technique"):
        paper.build_node(goog_lab, [SignalSpec("orb")], ["GOOG"])


# ----------------------------------------------------------------------------- volume profile


def test_value_area_holds_most_volume_around_the_point_of_control():
    from lab.signals.builtin import value_area

    # Most volume trades at 100-101; thin tails to 95 and 106.
    high = np.array([101.0] * 8 + [96.0, 106.0])
    low = np.array([100.0] * 8 + [95.0, 105.0])
    vol = np.array([1000.0] * 8 + [50.0, 50.0])
    val, poc, vah = value_area(high, low, vol)
    step = (106 - 95) / 50  # one price bin
    assert val == pytest.approx(100.0, abs=step) and vah == pytest.approx(101.0, abs=step)
    assert val <= poc <= vah
    assert value_area(np.array([1.0]), np.array([1.0]), np.array([0.0])) is None


def _two_sessions(today_closes):
    """A previous session with its value at 100-101, then today's bars (15m)."""

    prev_h, prev_l = [101.0] * 24 + [96.0, 106.0], [100.0] * 24 + [95.0, 105.0]
    c = np.array([100.5] * 26 + list(today_closes))
    h = np.array(prev_h + [x + 0.1 for x in today_closes])
    lo = np.array(prev_l + [x - 0.1 for x in today_closes])
    v = np.array([1000.0] * 24 + [50.0, 50.0] + [500.0] * len(today_closes))
    dates = np.array([np.datetime64("2026-03-02")] * 26 + [np.datetime64("2026-03-03")] * len(today_closes))
    minute = np.concatenate([15.0 * np.arange(1, 27), 15.0 * np.arange(1, len(today_closes) + 1)])
    return c, h, lo, v, dates, minute


def _feed_va(sig, today):
    from lab.signals.base import Window

    c, h, lo, v, dates, minute = _two_sessions(today)
    out = []
    for i in range(27, len(c) + 1):
        t = sig.target(Window(c[:i], h[:i], lo[:i], c[:i], v[:i], dates[:i], minute[:i]))
        sig.last = t
        out.append(t)
    return out


def test_value_area_failed_auction_below_goes_long_to_the_far_edge():
    from lab.signals.builtin import BUILTIN

    # Opens below value (99), comes back inside for two bars (long), then reaches the top edge (out).
    targets = _feed_va(BUILTIN["value_area"](), [99.0, 99.2, 100.3, 100.5, 100.8, 101.2, 100.5])
    assert targets[:3] == [0, 0, 0]  # outside, then the first bar back inside: not yet confirmed
    assert targets[3:5] == [1, 1]  # two closes back in value: long, held inside
    assert targets[5:] == [0, 0]  # reached the value area high: out, and no second trade today


def test_value_area_failed_auction_above_goes_short_and_stops_out():
    from lab.signals.builtin import BUILTIN

    targets = _feed_va(BUILTIN["value_area"](), [102.0, 100.8, 100.6, 101.6])
    assert targets[2] == -1  # came back from above: short toward the low edge
    assert targets[3] == 0  # closed back above the value area: stopped
    assert _feed_va(BUILTIN["value_area"](short=0), [102.0, 100.8, 100.6])[-1] == 0


def test_value_area_needs_an_excursion_first():
    from lab.signals.builtin import BUILTIN

    assert set(_feed_va(BUILTIN["value_area"](), [100.4, 100.5, 100.6, 100.5])) == {0}


def test_value_area_direction_comes_from_the_latest_failed_excursion():
    """ChatGPT C005-F1: below, then above, then back inside is a failed auction ABOVE: short, not long."""
    from lab.signals.builtin import BUILTIN

    assert _feed_va(BUILTIN["value_area"](), [99.0, 102.0, 100.5, 100.6])[-1] == -1
    assert _feed_va(BUILTIN["value_area"](), [102.0, 99.0, 100.5, 100.6])[-1] == 1
    assert _feed_va(BUILTIN["value_area"](short=0), [99.0, 102.0, 100.5, 100.6])[-1] == 0


def test_a_zero_range_bar_keeps_its_volume():
    """ChatGPT C005-F2: a bar with high == low on a bin boundary lost its volume (and divided by zero)."""
    from lab.signals.builtin import value_area

    with np.errstate(divide="raise", invalid="raise"):
        val, poc, vah = value_area(np.array([20.0, 15.0]), np.array([10.0, 15.0]), np.array([1.0, 100.0]), bins=10)
    assert val <= 15.0 <= vah and abs(poc - 15.0) <= 1.0  # the 100-share print at 15 is the point of control
