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
