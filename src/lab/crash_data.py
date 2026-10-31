"""
Minute history for crash research: 1-minute klines and 5-minute open interest of Binance USD-M perpetuals, from
Binance's free public archive (data.binance.vision, no keys).

The recorder holds days of books; crash research needs years, because crashes are rare. The archive serves one
zip per symbol and period: klines per month (and per day for the current month), metrics (open interest) per day.
`fetch` downloads every period not yet cached into one Parquet file per period under <data>/crash/, so an
interrupted download resumes where it stopped; `load` reads a symbol back as one frame.

  klines  ts: the end of the minute (open time + 60 s), open, high, low, close, quote_volume (USDT),
          taker_buy_quote (USDT bought by takers in the minute)
  oi      ts: the snapshot time, oi_value: the open interest in USDT

A period the archive doesn't have (before the listing, after a delisting) is remembered as missing once it is old
enough that the archive would have published it, and is not asked for again.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd

from lab.carry_data import patient

ARCHIVE = "https://data.binance.vision/data/futures/um"
KINDS = ("klines", "oi")
MINUTE_MS = 60_000
PUBLISH_LAG = timedelta(days=3)  # the archive publishes a day's file within a day or two; older and absent = missing
KLINE_COLUMNS = ("open", "high", "low", "close", "quote_volume", "taker_buy_quote")


def get_bytes(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "trading-lab"}), timeout=60) as r:
        return r.read()


def url_of(kind: str, symbol: str, period: str) -> str:
    """period: YYYY-MM (a month of klines) or YYYY-MM-DD (a day of klines or of metrics)."""
    if kind == "klines":
        span = "monthly" if len(period) == 7 else "daily"
        return f"{ARCHIVE}/{span}/klines/{symbol}/1m/{symbol}-1m-{period}.zip"
    return f"{ARCHIVE}/daily/metrics/{symbol}/{symbol}-metrics-{period}.zip"


def _csv(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z, z.open(z.namelist()[0]) as f:
        text = f.read()
    first = text.split(b"\n", 1)[0]
    has_header = not first[:1].isdigit()  # older archive files have no header row
    return pd.read_csv(io.BytesIO(text), header=0 if has_header else None)


def _ms(x: pd.Series) -> pd.Series:
    """Epoch times in ms; the archive moved some files to microseconds."""
    x = x.astype("int64")
    return x.where(x < 10**14, x // 1000)


def parse_klines(blob: bytes) -> pd.DataFrame:
    df = _csv(blob)
    # open_time, open, high, low, close, volume, close_time, quote_volume, count, taker_buy_volume,
    # taker_buy_quote_volume, ignore
    cols = df.columns
    out = pd.DataFrame({"ts": pd.to_datetime(_ms(df[cols[0]]) + MINUTE_MS, unit="ms", utc=True)})
    for name, k in zip(KLINE_COLUMNS, (1, 2, 3, 4, 7, 10), strict=True):
        out[name] = df[cols[k]].astype(float).to_numpy()
    return out


def parse_metrics(blob: bytes) -> pd.DataFrame:
    df = _csv(blob)  # create_time, symbol, sum_open_interest, sum_open_interest_value, ...
    return pd.DataFrame(
        {
            "ts": pd.to_datetime(df["create_time"], utc=True),
            "oi_value": df["sum_open_interest_value"].astype(float).to_numpy(),
        }
    )


def periods(kind: str, since: date, today: date) -> list[str]:
    """Every archive period from `since` to yesterday: whole months of klines before this month, then days."""
    out, d = [], since
    if kind == "klines":
        month = date(since.year, since.month, 1)
        this_month = date(today.year, today.month, 1)
        while month < this_month:
            out.append(f"{month:%Y-%m}")
            month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        d = max(since, this_month)
    while d < today:
        out.append(f"{d:%Y-%m-%d}")
        d += timedelta(days=1)
    return out


def _dir(root: Path, kind: str, symbol: str) -> Path:
    return Path(root) / kind / symbol


def _end_of(period: str) -> date:
    if len(period) == 7:
        y, m = map(int, period.split("-"))
        return date(y + m // 12, m % 12 + 1, 1)
    return date.fromisoformat(period) + timedelta(days=1)


def _fetch(d: Path, kind: str, symbol: str, period: str, today: date, get) -> bool | None:
    """Caches one period: True if added, None if the archive doesn't have it (yet), False if cached already."""
    done, missing = d / f"{period}.parquet", d / f"{period}.missing"
    if done.exists() or missing.exists():
        return False
    try:
        blob = get(url_of(kind, symbol, period))
    except HTTPError as e:
        if e.code != 404:
            raise
        if today - _end_of(period) >= PUBLISH_LAG:
            missing.touch()
        return None
    tmp = done.with_name(done.name + ".tmp")
    (parse_klines if kind == "klines" else parse_metrics)(blob).to_parquet(tmp, index=False)
    tmp.replace(done)
    return True


def update(
    root: Path, kind: str, symbol: str, since: date, today: date | None = None, get=get_bytes
) -> tuple[int, int]:
    """Downloads every period not cached yet. Returns (files added, files cached in all)."""
    today = today or datetime.now(UTC).date()
    get = patient(get)
    d = _dir(root, kind, symbol)
    d.mkdir(parents=True, exist_ok=True)
    added = 0
    for period in periods(kind, since, today):
        got = _fetch(d, kind, symbol, period, today, get)
        if got is None and len(period) == 7 and not (d / f"{period}.missing").exists():
            # last month's file isn't published yet: its days stand in (load keeps a minute once)
            y, m = map(int, period.split("-"))
            days = [p for p in periods("oi", max(since, date(y, m, 1)), _end_of(period))]
            added += sum(bool(_fetch(d, kind, symbol, day, today, get)) for day in days)
        added += bool(got)
    return added, len(list(d.glob("*.parquet")))


def load(root: Path, kind: str, symbol: str) -> pd.DataFrame | None:
    """Every cached period of a symbol as one frame sorted by ts (a month and its days overlap: kept once)."""
    files = sorted(_dir(root, kind, symbol).glob("*.parquet"))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    df = df.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
    return df


def cached_symbols(root: Path) -> list[str]:
    return sorted(p.name for p in (Path(root) / "klines").glob("*") if p.is_dir() and any(p.glob("*.parquet")))


def last_cached(root: Path) -> pd.Timestamp | None:
    """The end of the latest cached kline period of any symbol (no file is opened)."""
    last = None
    for p in (Path(root) / "klines").glob("*/*.parquet"):
        end = pd.Timestamp(_end_of(p.stem), tz="UTC")
        last = end if last is None else max(last, end)
    return last
