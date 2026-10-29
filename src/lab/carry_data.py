"""
Hourly history for funding-carry research: funding settlements and hourly closes of Binance USD-M perpetuals,
Binance spot and Bybit linear perpetuals, from the venues' public REST APIs (no keys).

The recorder's catalog holds days of funding; carry needs years, which the exchanges serve for free. `fetch`
downloads missing hourly intervals and funding after the last event into one Parquet file per series under
<data>/carry/; `load` reads it back. Hourly close/turnover holes are retried, never filled with invented values.

Each series is (ts, value), ts in UTC nanoseconds:
  funding  ts: the settlement time, value: the rate paid by longs to shorts at it (fraction of notional)
  close    ts: the end of the hour, value: the last trade price of the hour that ends there
  turnover ts: the end of the hour, value: the hour's traded value in USDT (stored with `close`, from the same klines)
Klines still open are never stored.
"""

from __future__ import annotations

import math
import time
from itertools import pairwise
from pathlib import Path
from urllib.error import HTTPError, URLError

import pandas as pd

from lab.persistence import file_lock
from lab.survey import binance_perpetuals, get_json

BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"
BINANCE_SPOT = "https://api.binance.com/api/v3"
BYBIT_V5 = "https://api.bybit.com/v5/market"
HOUR_MS = 3_600_000
# Waits of 2, 4, ... 64 s, then 120 s: about 10 minutes in all, so rate limits, timeouts and a DNS or Wi-Fi blip pass.
TRIES = 10
MAX_WAIT_S = 120
BYBIT_RATE_LIMITED = {10006, 10018}
VENUES = ("binance", "bybit")
# (venue, market) -> what carry research reads from it
SERIES = {
    ("binance", "perp"): ("funding", "close"),
    ("binance", "spot"): ("close",),
    ("bybit", "perp"): ("funding", "close"),
}


class RateLimited(TimeoutError):
    """The venue asked us to slow down; retried, and like a timeout once the retries are spent."""


def patient(get, sleep=time.sleep, tries=TRIES):
    """`get` retried with exponential backoff on rate limits (HTTP 418/429, Bybit's HTTP-200 codes), server errors
    and network timeouts. Other HTTP errors (an unknown symbol) are answers, not failures, and pass through."""

    def call(url):
        for k in range(tries):
            try:
                r = get(url)
                if isinstance(r, dict) and r.get("retCode") in BYBIT_RATE_LIMITED:
                    raise RateLimited(f"bybit: {r.get('retMsg')}")
                return r
            except HTTPError as e:
                if (e.code not in (418, 429) and e.code < 500) or k == tries - 1:
                    raise
            except (URLError, TimeoutError):
                if k == tries - 1:
                    raise
            sleep(min(2 ** (k + 1), MAX_WAIT_S))
        raise AssertionError("unreachable")

    return call


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.value // 1_000_000)


def path_of(root: Path, venue: str, market: str, kind: str, symbol: str) -> Path:
    return Path(root) / f"{venue}-{market}-{kind}-{symbol}.parquet"


def _page_times(page, key, start, end, limit, *, reverse=False, hourly=False):
    """Reject repeated/out-of-window pages before advancing a cursor or touching the cache."""
    try:
        if not isinstance(page, list):
            raise ValueError("expected a list")
        raw = [key(row) for row in page]
        if any(type(t) not in (int, str) for t in raw):
            raise ValueError("expected integer milliseconds")
        times = [int(t) for t in raw]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ValueError("invalid history page: malformed timestamp records") from e
    ordered = list(reversed(times)) if reverse else times
    if (
        len(times) > limit
        or any(t < start or t > end or (hourly and t % HOUR_MS) for t in times)
        or any(a >= b for a, b in pairwise(ordered))
    ):
        raise ValueError("invalid history page: duplicate, unordered, unaligned or out-of-window timestamps")
    return times


def _value(value, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError("invalid history value: boolean is not a price or rate")
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError("invalid history value: expected finite funding, positive close or nonnegative turnover")
    return value


def _binance_funding(symbol, start_ms, now_ms, get, pause):
    rows = []
    while start_ms <= now_ms:
        page = get(f"{BINANCE_FAPI}/fundingRate?symbol={symbol}&startTime={start_ms}&endTime={now_ms}&limit=1000")
        times = _page_times(page, lambda r: r["fundingTime"], start_ms, now_ms, 1000)
        rows += [(t, _value(r["fundingRate"])) for t, r in zip(times, page, strict=True)]
        if len(page) < 1000:
            break
        start_ms = times[-1] + 1
        pause()
    return rows


def _binance_closes(url, limit, start_ms, now_ms, get, pause):
    rows = []
    end_ms = now_ms - HOUR_MS  # native API bounds refer to opens; do not request the unfinished candle
    while start_ms <= end_ms:
        page = get(f"{url}&interval=1h&startTime={start_ms}&endTime={end_ms}&limit={limit}")
        times = _page_times(page, lambda k: k[0], start_ms, end_ms, limit, hourly=True)
        # [open time, open, high, low, close, volume, close time, quote volume, ...]; the hour ends at open + 1 h.
        # The open kline is left out.
        rows += [
            (t + HOUR_MS, _value(k[4], positive=True), _value(k[7], nonnegative=True))
            for t, k in zip(times, page, strict=True)
        ]
        if len(page) < limit:
            break
        start_ms = times[-1] + HOUR_MS
        pause()
    return rows


def _bybit_list(get, url) -> list:
    r = get(url)
    if r.get("retCode"):  # Bybit answers errors (e.g. an unknown symbol) with HTTP 200
        raise ValueError(f"bybit: {r.get('retMsg')}")
    return r["result"]["list"]


def _bybit_pages(url, key, start_ms, now_ms, get, pause, limit):
    """Bybit pages newest first: walk backwards from now to start_ms."""
    rows, end_ms = [], now_ms
    while end_ms >= start_ms:
        page = _bybit_list(get, f"{url}&startTime={start_ms}&endTime={end_ms}&limit={limit}")
        times = _page_times(page, key, start_ms, end_ms, limit, reverse=True)
        rows += page
        if len(page) < limit:
            break
        end_ms = times[-1] - 1
        pause()
    return rows


def _bybit_funding(symbol, start_ms, now_ms, get, pause):
    page = _bybit_pages(
        f"{BYBIT_V5}/funding/history?category=linear&symbol={symbol}",
        lambda r: r["fundingRateTimestamp"],
        start_ms,
        now_ms,
        get,
        pause,
        200,
    )
    return [(int(r["fundingRateTimestamp"]), _value(r["fundingRate"])) for r in page]


def _bybit_closes(symbol, start_ms, now_ms, get, pause):
    # kline rows are [start, open, high, low, close, volume, turnover]; `start`/`end` name the window there
    rows, end_ms = [], now_ms - HOUR_MS
    while end_ms >= start_ms:
        page = _bybit_list(
            get,
            f"{BYBIT_V5}/kline?category=linear&symbol={symbol}&interval=60&start={start_ms}&end={end_ms}&limit=1000",
        )
        times = _page_times(page, lambda k: k[0], start_ms, end_ms, 1000, reverse=True, hourly=True)
        rows += [
            (t + HOUR_MS, _value(k[4], positive=True), _value(k[6], nonnegative=True))
            for t, k in zip(times, page, strict=True)
        ]
        if len(page) < 1000:
            break
        end_ms = times[-1] - 1
        pause()
    return rows


def download(
    venue, market, kind, symbol, start_ms, now_ms, get=get_json, pause=lambda: time.sleep(0.5), sleep=time.sleep
):
    """Funding events in [start_ms, now_ms]; candle opens >=start_ms with closes <=now_ms.

    Close rows use hour-END timestamps, unlike the native candle request bounds.
    """
    if kind not in SERIES.get((venue, market), ()):
        raise ValueError(f"unknown series: {venue} {market} {kind}")
    get = patient(get, sleep)
    if (venue, market) == ("binance", "perp"):
        if kind == "funding":
            return _binance_funding(symbol, start_ms, now_ms, get, pause)
        return _binance_closes(f"{BINANCE_FAPI}/klines?symbol={symbol}", 1500, start_ms, now_ms, get, pause)
    if (venue, market) == ("binance", "spot"):
        return _binance_closes(f"{BINANCE_SPOT}/klines?symbol={symbol}", 1000, start_ms, now_ms, get, pause)
    if (venue, market) == ("bybit", "perp"):
        if kind == "funding":
            return _bybit_funding(symbol, start_ms, now_ms, get, pause)
        return _bybit_closes(symbol, start_ms, now_ms, get, pause)
    raise ValueError(f"unknown series: {venue} {market} {kind}")


def load(root: Path, venue: str, market: str, kind: str, symbol: str) -> pd.Series:
    """The cached series (float, indexed by UTC timestamp), empty if never fetched."""
    p = path_of(root, venue, market, kind, symbol)
    if not p.exists():
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"), name=kind)
    df = pd.read_parquet(p)
    return pd.Series(df["value"].to_numpy(), index=pd.to_datetime(df["ts"], utc=True), name=kind)


def _valid_cache(series, kind):
    if not series.index.is_unique or not series.index.is_monotonic_increasing or series.index.hasnans:
        raise ValueError("invalid history cache: timestamps must be ordered, unique and nonmissing")
    for ts, value in series.items():
        if kind != "funding" and ts != ts.floor("h"):
            raise ValueError("invalid history cache: hourly timestamps must be aligned")
        _value(value, positive=kind == "close", nonnegative=kind == "turnover")


def _ranges(index):
    """Inclusive consecutive hour-end ranges; no placeholder rows or forward filling."""
    ranges = []
    for ts in index:
        if ranges and ts == ranges[-1][1] + pd.Timedelta(hours=1):
            ranges[-1] = (ranges[-1][0], ts)
        else:
            ranges.append((ts, ts))
    return ranges


def _hourly_grid(old, since, now):
    first = since.ceil("h") + pd.Timedelta(hours=1)
    # Before the first observed candle availability is unknown (e.g. before listing). Do not repeatedly
    # download that prefix. An absent companion file does need a full backfill from the requested start.
    if all(len(o) for o in old):
        first = max(first, min(o.index[0] for o in old))
    return pd.date_range(first, now.floor("h"), freq="h")


def _window(since, now):
    if pd.isna(since) or pd.isna(now) or since.tzinfo is None or now.tzinfo is None or since > now:
        raise ValueError("history window requires timezone-aware since <= now")
    return since.tz_convert("UTC"), now.tz_convert("UTC")


def close_coverage(root, venue, market, symbol, since, now):
    """Report hourly gaps through the frozen cutoff. Funding schedules are NOT inferred from candle coverage."""
    since, now = _window(since, now)
    path = path_of(root, venue, market, "close", symbol)
    with file_lock(path.with_name(path.name + ".lock")):
        old = [load(root, venue, market, kind, symbol) for kind in ("close", "turnover")]
        for kind, series in zip(("close", "turnover"), old, strict=True):
            _valid_cache(series, kind)
    grid = _hourly_grid(old, since, now)
    requested = pd.date_range(since.ceil("h") + pd.Timedelta(hours=1), now.floor("h"), freq="h")
    observed = [o.index.intersection(requested) for o in old]
    first = min((idx[0] for idx in observed if len(idx)), default=None)
    # A missing file must remain visible, while an unknown pre-listing prefix is labelled separately.
    known = grid[grid >= first] if first is not None else grid[:0]
    series = {}
    for kind, o in zip(("close", "turnover"), old, strict=True):
        gaps = known.difference(o.index)
        series[kind] = {
            "rows": int(o.index.isin(known).sum()),
            "missing_hours": len(gaps),
            "gaps": [
                {"from": a.isoformat(), "through": b.isoformat(), "hours": int((b - a) / pd.Timedelta(hours=1)) + 1}
                for a, b in _ranges(gaps)
            ],
        }
    return {
        "version": 1,
        "venue": venue,
        "market": market,
        "symbol": symbol,
        "since": since.isoformat(),
        "through": now.floor("h").isoformat(),
        "observed_from": first.isoformat() if first is not None else None,
        "leading_unverified_hours": int((requested < first).sum()) if first is not None else len(requested),
        "state": "unavailable"
        if first is None
        else ("gappy" if any(s["missing_hours"] for s in series.values()) else "continuous-observed-window"),
        "series": series,
        "funding_schedule_verified": False,
    }


def update(root: Path, venue, market, kind, symbol, since: pd.Timestamp, now: pd.Timestamp, **io) -> tuple[int, int]:
    """Append funding; repair missing hourly close/turnover ranges as well as the tail, without invented values.
    Returns (new rows, total rows) of `kind`."""
    kinds = (kind, "turnover") if kind == "close" else (kind,)
    since, now = _window(since, now)
    if kind not in SERIES.get((venue, market), ()):
        raise ValueError(f"unknown series: {venue} {market} {kind}")
    p = path_of(root, venue, market, kind, symbol)
    with file_lock(p.with_name(p.name + ".lock")):
        old = [load(root, venue, market, k, symbol) for k in kinds]
        for k, o in zip(kinds, old, strict=True):
            _valid_cache(o, k)
        if kind == "close":
            grid = _hourly_grid(old, since, now)
            present = old[0].index.intersection(old[1].index)
            windows = [(a - pd.Timedelta(hours=1), b) for a, b in _ranges(grid.difference(present))]
        else:
            start = max(since, old[0].index[-1] + pd.Timedelta(milliseconds=1)) if len(old[0]) else since
            windows = [(start, now)] if start <= now else []
        rows = [row for a, b in windows for row in download(venue, market, kind, symbol, _ms(a), _ms(b), **io)]
        index = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
        counts = []
        for col, (k, o) in enumerate(zip(kinds, old, strict=True), start=1):
            new = pd.Series([r[col] for r in rows], index=index, dtype=float)
            both = pd.concat([o, new]).sort_index()
            both = both[~both.index.duplicated(keep="last")]
            if len(new):
                q = path_of(root, venue, market, k, symbol)
                q.parent.mkdir(parents=True, exist_ok=True)
                tmp = q.with_name(q.name + ".tmp")
                pd.DataFrame({"ts": both.index, "value": both.to_numpy()}).to_parquet(tmp, index=False)
                tmp.replace(q)
            counts.append((len(both) - len(o), len(both)))
        return counts[0]


def cached_symbols(root: Path) -> list[str]:
    """Every symbol with Binance perpetual funding in the cache."""
    return sorted(
        p.name.removeprefix("binance-perp-funding-").removesuffix(".parquet")
        for p in Path(root).glob("binance-perp-funding-*.parquet")
    )


def universe(get=get_json) -> list[str]:
    """Every USDT pair trading today both as a Binance USD-M perpetual and on Binance spot under the same name
    (so not 1000PEPEUSDT, whose spot pair is PEPEUSDT). Today's list: delisted pairs are missing."""
    get = patient(get)
    spot = {
        s["symbol"]
        for s in get(f"{BINANCE_SPOT}/exchangeInfo?permissions=SPOT")["symbols"]
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
    }
    return sorted(binance_perpetuals(get) & spot)
