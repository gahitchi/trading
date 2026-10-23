"""
Trading speeds and intraday bars.

Speeds: "daily" (decide after the close, fill at the next open), and the quick speeds "1h", "15m" and "5m"
(decide at every bar's close, fill at the next bar's open). Quick speeds are allowed only for the dynamic and
aggressive risk profiles (Gabriel, C003).

Intraday bars are stored per speed in data/intraday/<speed>/<TICKER>.parquet with their own manifest. Each bar
is indexed by its START in New York time, regular trading hours only (09:30-16:00); its close is
min(start + interval, 16:00), so the last hourly bar of a session is 30 minutes. Early closes and holidays follow the pinned exchange calendar.

Free history is short (Yahoo Finance: about 2 years of 1h bars, about 60 days of 15m and 5m), so intraday
statistics rest on far less data than daily ones; every intraday result says how many sessions it used.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from lab.config import LabConfig
from lab.data import COLUMNS, DataError, validate
from lab.exchange import bounds

OPEN, CLOSE = time(9, 30), time(16, 0)
QUICK_PROFILES = {"dynamic", "aggressive"}


@dataclass(frozen=True)
class Speed:
    name: str
    minutes: int | None  # None = daily
    yahoo_interval: str | None
    yahoo_period: str | None  # the longest history Yahoo serves at this interval

    @property
    def daily(self) -> bool:
        return self.minutes is None

    @property
    def bars_per_session(self) -> int:
        if self.minutes is None:
            return 1
        return -(-390 // self.minutes)  # 6.5 hours, the last bar possibly short

    def bar_spec(self) -> str:
        """NautilusTrader bar specification: 1-DAY, 1-HOUR, 15-MINUTE, 5-MINUTE."""
        if self.minutes is None:
            return "1-DAY"
        return "1-HOUR" if self.minutes == 60 else f"{self.minutes}-MINUTE"


SPEEDS = {
    "daily": Speed("daily", None, None, None),
    "1h": Speed("1h", 60, "60m", "730d"),
    "15m": Speed("15m", 15, "15m", "60d"),
    "5m": Speed("5m", 5, "5m", "60d"),
}


def speed(name: str) -> Speed:
    if name not in SPEEDS:
        raise ValueError(f"unknown speed {name!r}; choose one of {', '.join(SPEEDS)}")
    return SPEEDS[name]


def check_speed_allowed(name: str, profile: str) -> None:
    """Quick speeds need a profile that permits them (Gabriel: dynamic and aggressive)."""
    sp = speed(name)
    if not sp.daily and profile not in QUICK_PROFILES:
        raise ValueError(
            f"speed {name!r} is a quick speed: it needs the dynamic or aggressive profile "
            f"([risk] profile is {profile!r})"
        )


# ----------------------------------------------------------------------------- bar times


def bar_end(start: pd.Timestamp, minutes: int) -> pd.Timestamp:
    """Close of the bar that starts at `start`: start + interval, cut at the 16:00 close."""
    end = start + pd.Timedelta(minutes=minutes)
    hours = bounds(start.date())
    if hours is None:
        raise DataError(f"bar on a non-session: {start}")
    close = pd.Timestamp(hours[1]).tz_localize(None)
    return min(end, close)


def in_closing_window(end: pd.Timestamp, minutes: int, n: int = 1) -> bool:
    """True if at most n intervals of the session remain after a bar that closed at `end`: an order decided now
    gets at most n bars to fill before 16:00. Flat-at-close exits here (n=1 leaves the session's last bar to
    fill the exit; a decision at 16:00 itself has none)."""
    hours = bounds(end.date())
    if hours is None:
        return True
    close = pd.Timestamp(hours[1]).tz_localize(None)
    return close - end <= pd.Timedelta(minutes=minutes * n)


def session_bars(day: pd.Timestamp, minutes: int) -> list[pd.Timestamp]:
    """Bar starts of a regular session."""
    t = day.normalize() + pd.Timedelta(hours=9, minutes=30)
    hours = bounds(day.date())
    if hours is None:
        return []
    close = pd.Timestamp(hours[1]).tz_localize(None)
    out = []
    while t < close:
        out.append(t)
        t += pd.Timedelta(minutes=minutes)
    return out


# ----------------------------------------------------------------------------- storage


def _dir(cfg: LabConfig, sp: Speed) -> Path:
    return cfg.data_dir / "intraday" / sp.name


def manifest(cfg: LabConfig, sp: Speed) -> dict:
    p = _dir(cfg, sp) / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def validate_intraday(df: pd.DataFrame, ticker: str, sp: Speed, *, drop_incomplete: bool = False) -> pd.DataFrame:
    """The daily checks (finite, positive, consistent OHLC, no duplicates) plus: regular hours only and on the
    speed's bar grid. Bars outside 09:30-16:00 are refused, not silently dropped (pre/post-market data is a
    different market)."""
    if sp.daily:
        raise ValueError("validate_intraday is for quick speeds")
    out = validate(df, ticker, drop_incomplete=drop_incomplete, keep_time=True)
    tod = out.index - out.index.normalize()
    open_ = pd.Timedelta(hours=9, minutes=30)
    actual_closes = []
    for stamp in out.index:
        hours = bounds(stamp.date())
        if hours is None:
            raise DataError(f"{ticker}: non-session bar {stamp}")
        actual_closes.append(pd.Timestamp(hours[1]).tz_localize(None) - stamp.normalize())
    outside = (tod < open_) | (tod >= pd.TimedeltaIndex(actual_closes))
    if outside.any():
        raise DataError(f"{ticker}: {int(outside.sum())} bar(s) outside regular hours, e.g. {out.index[outside][0]}")
    off_grid = ((tod - open_) % pd.Timedelta(minutes=sp.minutes)) != pd.Timedelta(0)
    if off_grid.any():
        raise DataError(f"{ticker}: bar(s) not on the {sp.name} grid from 09:30, e.g. {out.index[off_grid][0]}")
    return out


def save(cfg: LabConfig, ticker: str, df: pd.DataFrame, sp: Speed, *, source: str, adjusted: bool) -> dict:
    ticker = ticker.upper()
    clean = validate_intraday(df, ticker, sp, drop_incomplete=True)
    dropped = clean.attrs.get("dropped_incomplete", [])
    d = _dir(cfg, sp)
    d.mkdir(parents=True, exist_ok=True)
    old_path = d / f"{ticker}.parquet"
    m = manifest(cfg, sp)
    rebased = None
    if old_path.exists() and m.get(ticker, {}).get("adjusted") == adjusted:
        # Keep what was fetched before: free vendors serve only the last weeks, so history grows by fetching
        # regularly (at least every few weeks, so the copies overlap). Newer bars replace older copies.
        old = validate_intraday(pd.read_parquet(old_path), ticker, sp)
        both = old.index.intersection(clean.index)
        if len(both):
            # Adjusted prices are rebased by every later split and dividend: scale the kept history onto the new
            # fetch's basis, measured on the bars both copies share.
            ratio = float((clean.loc[both, "close"] / old.loc[both, "close"]).median())
            if abs(ratio - 1) > 1e-6:
                old = old.assign(**{k: old[k] * ratio for k in ("open", "high", "low", "close")})
                old = old.assign(volume=old["volume"] / ratio)
                rebased = ratio
            clean = pd.concat([old[~old.index.isin(clean.index)], clean]).sort_index()
            dropped = sorted(set(m[ticker].get("dropped_incomplete", [])) | set(dropped))
        # No shared bar: the stored copy cannot be put on the new basis, so it is replaced, not stitched.
    clean.to_parquet(d / f"{ticker}.parquet")
    sessions = clean.index.normalize().unique()
    entry = {
        "source": source,
        "adjusted": adjusted,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "first": str(clean.index[0]),
        "last": str(clean.index[-1]),
        "rows": len(clean),
        "sessions": len(sessions),
        "dropped_incomplete": dropped,
        "repaired_high_low": clean.attrs.get("repaired_high_low", []),
        "rebased_history_by": rebased,
    }
    m[ticker] = entry
    (d / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True))
    return entry


def load(cfg: LabConfig, ticker: str, sp: Speed) -> pd.DataFrame:
    p = _dir(cfg, sp) / f"{ticker.upper()}.parquet"
    if not p.exists():
        raise DataError(f"no {sp.name} data for {ticker}; run: lab data fetch {ticker} --speed {sp.name}")
    return validate_intraday(pd.read_parquet(p), ticker, sp)


def load_many(cfg: LabConfig, tickers, sp: Speed) -> dict[str, pd.DataFrame]:
    return {t: load(cfg, t, sp) for t in tickers}


def fetch_yfinance(ticker: str, sp: Speed) -> pd.DataFrame:
    """The longest intraday history Yahoo serves at this speed, adjusted, regular hours, bar starts in New York."""
    import yfinance as yf

    if sp.daily:
        raise ValueError("use data.fetch_yfinance for daily bars")
    try:
        df = yf.Ticker(ticker).history(
            period=sp.yahoo_period,
            interval=sp.yahoo_interval,
            auto_adjust=True,
            actions=False,
            prepost=False,
            raise_errors=True,
        )
    except Exception as e:  # yfinance raises a variety of types
        raise DataError(f"{ticker}: Yahoo Finance {sp.name} download failed: {e}") from e
    if df is None or df.empty:
        raise DataError(f"{ticker}: Yahoo Finance returned no {sp.name} data")
    df = df.rename(columns=str.lower)[COLUMNS]
    idx = pd.DatetimeIndex(df.index)
    df.index = (idx.tz_convert("America/New_York") if idx.tz is not None else idx).tz_localize(None)
    tod = df.index - df.index.normalize()
    keep = (tod >= pd.Timedelta(hours=9, minutes=30)) & (tod < pd.Timedelta(hours=16))
    return df[keep]


def history_note(frames: dict[str, pd.DataFrame], sp: Speed) -> str:
    sessions = min((f.index.normalize().nunique() for f in frames.values()), default=0)
    return (
        f"{sp.name} bars: {sessions} sessions of history (daily research uses years); intraday statistics are "
        "much weaker"
    )


def now_session_bounds(now: datetime) -> tuple[datetime, datetime]:
    d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return d + timedelta(hours=9, minutes=30), d + timedelta(hours=16)


def check_technique(name, params, minutes, delay, flat_at_close):
    """Refuse a configured technique with no possible entry before liquidation."""
    from lab.signals.builtin import BUILTIN

    if name not in ("orb", "intraday_momentum", "vwap_reversion"):
        return
    p = {**BUILTIN[name].defaults, **params}
    if name == "orb":
        if minutes > p["range_min"]:
            raise ValueError("orb: bar interval exceeds the opening range; use finer bars or a longer range_min")
        first = (p["range_min"] // minutes + 1) * minutes
    elif name == "intraday_momentum":
        if minutes > p["first_min"]:
            raise ValueError("intraday_momentum: bars cannot resolve first_min; use finer bars")
        first = math.ceil(p["entry_min"] / minutes) * minutes
    else:
        first = max(4 * minutes, math.ceil(p["min_minutes"] / minutes) * minutes)
    cutoff = 390 - minutes if flat_at_close else 390
    if first + delay >= cutoff:
        raise ValueError(f"{name}: no executable entry window before liquidation/close at this interval and delay")
