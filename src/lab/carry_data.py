"""
Hourly history for funding-carry research: funding settlements and hourly closes of Binance USD-M perpetuals,
Binance spot and Bybit linear perpetuals, from the venues' public REST APIs (no keys).

The recorder's catalog holds days of funding; carry needs years, which the exchanges serve for free. `fetch`
downloads what is missing and appends it to one Parquet file per series under <data>/carry/; `load` reads it back.

Each series is (ts, value), ts in UTC nanoseconds:
  funding  ts: the settlement time, value: the rate paid by longs to shorts at it (fraction of notional)
  close    ts: the end of the hour, value: the last trade price of the hour that ends there
  turnover ts: the end of the hour, value: the hour's traded value in USDT (stored with `close`, from the same klines)
Klines still open are never stored.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote

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


def _binance_funding(symbol, start_ms, now_ms, get, pause):
    rows = []
    while start_ms < now_ms:
        page = get(f"{BINANCE_FAPI}/fundingRate?symbol={symbol}&startTime={start_ms}&limit=1000")
        rows += [(int(r["fundingTime"]), float(r["fundingRate"])) for r in page]
        if len(page) < 1000:
            break
        start_ms = int(page[-1]["fundingTime"]) + 1
        pause()
    return rows


def _binance_closes(url, limit, start_ms, now_ms, get, pause):
    rows = []
    while start_ms < now_ms:
        # ask for no more rows than the hours left: Binance weighs a request by its limit, and an hourly update of
        # every pair would otherwise spend the minute's budget
        n = min(limit, (now_ms - start_ms) // HOUR_MS + 2)
        page = get(f"{url}&interval=1h&startTime={start_ms}&limit={n}")
        # [open time, open, high, low, close, volume, close time, quote volume, ...]; the hour ends at open + 1 h.
        # The open kline is left out.
        rows += [(int(k[0]) + HOUR_MS, float(k[4]), float(k[7])) for k in page if int(k[0]) + HOUR_MS <= now_ms]
        if len(page) < n:
            break
        start_ms = int(page[-1][0]) + HOUR_MS
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
        rows += page
        if len(page) < limit:
            break
        end_ms = min(key(r) for r in page) - 1
        pause()
    return rows


def _bybit_funding(symbol, start_ms, now_ms, get, pause):
    page = _bybit_pages(
        f"{BYBIT_V5}/funding/history?category=linear&symbol={symbol}",
        lambda r: int(r["fundingRateTimestamp"]),
        start_ms,
        now_ms,
        get,
        pause,
        200,
    )
    return [(int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in page]


def _bybit_closes(symbol, start_ms, now_ms, get, pause):
    # kline rows are [start, open, high, low, close, volume, turnover]; `start`/`end` name the window there
    rows, end_ms = [], now_ms
    while end_ms >= start_ms:
        page = _bybit_list(
            get,
            f"{BYBIT_V5}/kline?category=linear&symbol={symbol}&interval=60&start={start_ms}&end={end_ms}&limit=1000",
        )
        rows += [(int(k[0]) + HOUR_MS, float(k[4]), float(k[6])) for k in page if int(k[0]) + HOUR_MS <= now_ms]
        if len(page) < 1000:
            break
        end_ms = min(int(k[0]) for k in page) - 1
        pause()
    return rows


def download(
    venue, market, kind, symbol, start_ms, now_ms, get=get_json, pause=lambda: time.sleep(0.5), sleep=time.sleep
):
    """[(ts_ms, value)] from start_ms (inclusive) up to now_ms; [(ts_ms, close, turnover)] for closes."""
    get = patient(get, sleep)
    symbol = quote(symbol)  # Binance lists pairs such as 币安人生USDT; a URL must be ASCII
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


def update(root: Path, venue, market, kind, symbol, since: pd.Timestamp, now: pd.Timestamp, **io) -> tuple[int, int]:
    """Downloads what is missing after the cache's last row (or from `since`) and stores the union; `close`
    also stores `turnover` from the same klines (from `since` if only the closes were cached).
    Returns (new rows, total rows) of `kind`."""
    kinds = (kind, "turnover") if kind == "close" else (kind,)
    p = path_of(root, venue, market, kind, symbol)
    with file_lock(p.with_name(p.name + ".lock")):
        old = [load(root, venue, market, k, symbol) for k in kinds]
        start = min(o.index[-1] + pd.Timedelta(milliseconds=1) if len(o) else since for o in old)
        rows = download(venue, market, kind, symbol, _ms(start), _ms(now), **io)
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
