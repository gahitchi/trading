"""
Minute history for crash research: 1-minute klines and 5-minute open interest of Binance USD-M perpetuals, from
Binance's free public archive (data.binance.vision, no keys).

The recorder holds days of books; crash research needs years, because crashes are rare. The archive serves one
zip per symbol and period: klines per month (and per day for the current month), metrics (open interest) per day.
`fetch` downloads every period not yet cached into one Parquet file per period under <data>/crash/, so an
interrupted download resumes where it stopped; `load` reads a symbol back as one frame.

  klines  ts: the end of the minute (open time + 60 s), open, high, low, close, quote_volume (USDT),
          taker_buy_quote (USDT bought by takers in the minute)
  oi      ts: snapshot time, oi_qty: open-interest contract quantity, oi_value: USDT notional
          Signal filters MUST use oi_qty: notional OI moves mechanically with price.

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

import numpy as np
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


def parse_metrics(blob: bytes, *, quarantine_zero_pairs: bool = False) -> pd.DataFrame:
    """Parse OI metrics; ordinary ingestion rejects zeros without exception.

    The explicit, hash-pinned historical quarantine path may mark only
    (quantity == 0, notional == 0) pairs as unavailable measurements.
    Contract quantities become NaN; the original notional zeros are retained.
    Asymmetric zeros, negative numbers, nonfinite values remain invalid.
    """
    df = _csv(blob)
    qty = pd.to_numeric(df["sum_open_interest"], errors="raise").to_numpy(dtype=float)
    notional = pd.to_numeric(df["sum_open_interest_value"], errors="raise").to_numpy(dtype=float)
    if not (np.isfinite(qty).all() and np.isfinite(notional).all()):
        raise ValueError("nonfinite Binance open-interest snapshot")
    zero_pair = (qty == 0) & (notional == 0)
    if (qty < 0).any() or (notional < 0).any() or (
        ((qty <= 0) | (notional <= 0)) & ~zero_pair
    ).any():
        raise ValueError("open-interest quantity and notional must be positive")
    if zero_pair.any() and not quarantine_zero_pairs:
        raise ValueError("open-interest quantity and notional must be positive")
    ts = pd.to_datetime(df["create_time"], utc=True, errors="raise")
    if ts.isna().any() or ts.duplicated().any():
        raise ValueError("invalid or duplicate Binance open-interest timestamps")
    if quarantine_zero_pairs:
        if not zero_pair.any():
            raise ValueError("zero-pair quarantine requested without invalid zero-pair observations")
        qty = qty.copy()
        qty[zero_pair] = np.nan
        return pd.DataFrame({
            "ts": ts, "oi_qty": qty, "oi_value": notional, "oi_valid": ~zero_pair,
        })
    return pd.DataFrame({"ts": ts, "oi_qty": qty, "oi_value": notional})


def validate_oi_quantity(frame: pd.DataFrame) -> None:
    """Fail closed unless every OI row is positive or explicitly quarantined."""
    if "oi_qty" not in frame:
        raise ValueError("contract quantity missing: legacy open-interest cache")
    qty = pd.to_numeric(frame["oi_qty"], errors="raise").to_numpy(dtype=float)
    if "oi_valid" not in frame:
        if not np.isfinite(qty).all() or (qty <= 0).any():
            raise ValueError("contract quantity missing/invalid: unmarked open-interest snapshot")
        return
    flags = frame["oi_valid"]
    if flags.isna().any() or not pd.api.types.is_bool_dtype(flags.dtype):
        raise ValueError("invalid OI quality-mask datatype")
    valid = flags.to_numpy(dtype=bool)
    if not (~valid).any():
        raise ValueError("OI quality marker without any quarantined samples")
    if "oi_value" not in frame:
        raise ValueError("missing original OI notional for quality validation")
    value = pd.to_numeric(frame["oi_value"], errors="raise").to_numpy(dtype=float)
    if not np.isfinite(value).all() or (valid & ((qty <= 0) | ~np.isfinite(qty) | (value <= 0))).any():
        raise ValueError("invalid positive OI quality-mask row")
    if ((~valid) & (~np.isnan(qty) | (value != 0))).any():
        raise ValueError("quarantined OI must be NaN contract quantity and original zero notional")



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


def load(root: Path, kind: str, symbol: str, *, before: pd.Timestamp | None = None) -> pd.DataFrame | None:
    """Every cached period of a symbol as one frame sorted by ts (a month and its days overlap: kept once)."""
    files = sorted(_dir(root, kind, symbol).glob("*.parquet"))
    if before is not None:
        if kind != "oi":
            raise ValueError("before cutoff is only implemented for OI metrics")
        cutoff = pd.Timestamp(before)
        if cutoff.tzinfo is None:
            raise ValueError("OI cache cutoff must be UTC timezone-aware")
        # Only complete pre-holdout daily periods are touched.
        files = [path for path in files if _end_of(path.stem) <= cutoff.date()]
    if not files:
        return None
    parts = []
    for path in files:
        part = pd.read_parquet(path)
        if kind == "oi":
            try:
                validate_oi_quantity(part)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"legacy/invalid open-interest cache at {path}: {exc}; "
                    "original Binance archive must be verified before research"
                ) from exc
        parts.append(part)
    df = pd.concat(parts, ignore_index=True)
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



def legacy_oi_paths(root: Path, symbol: str, before: date) -> list[Path]:
    """Only daily OI caches entirely before the supplied UTC research boundary."""
    if before is None:
        raise ValueError("an explicit UTC cutoff is required to protect the holdout")
    paths = sorted(_dir(root, "oi", symbol).glob("*.parquet"))
    out = []
    for path in paths:
        if len(path.stem) != 10 or _end_of(path.stem) > before:
            continue
        df = pd.read_parquet(path)
        if "oi_qty" not in df or ("oi_valid" not in df and df["oi_qty"].isna().any()):
            out.append(path)
        else:
            # An invalid or unaudited marker is not eligible for silent retry.
            validate_oi_quantity(df)
    return out


def migrate_oi_quantity(
    root: Path,
    symbol: str,
    *,
    before: date,
    apply: bool = False,
    get=get_bytes,
) -> list[str]:
    """Explicit upgrade of pre-holdout cached metrics using the *original* daily ZIP.

    Dry-run is the default. Each replacement requires exactly the same
    timestamp set and notional values as the legacy file; a changed archive
    is not accepted as an in-place correction. Existing files are replaced
    atomically per period, and neither post-boundary caches nor research
    holdout metadata are accessed.
    """
    from lab.persistence import file_lock

    folder = _dir(root, "oi", symbol)
    if not folder.is_dir():
        return []
    with file_lock(folder / ".oi-quantity-migration.lock"):
        legacy = legacy_oi_paths(root, symbol, before)
        if not apply:
            return [path.name for path in legacy]
        get = patient(get)
        upgraded = []
        for path in legacy:
            old = pd.read_parquet(path).sort_values("ts").reset_index(drop=True)
            new = parse_metrics(get(url_of("oi", symbol, path.stem))).sort_values("ts").reset_index(drop=True)
            if len(old) != len(new) or not pd.to_datetime(old["ts"], utc=True).equals(new["ts"]):
                raise ValueError(f"source timestamps changed for {path}; refusing migration")
            old_value = old["oi_value"].to_numpy(dtype=float)
            new_value = new["oi_value"].to_numpy(dtype=float)
            if not np.isfinite(old_value).all() or not np.allclose(old_value, new_value, rtol=1e-10, atol=1e-8):
                raise ValueError(f"source notional OI changed for {path}; refusing migration")
            replacement = old.copy()
            replacement["oi_qty"] = new["oi_qty"].to_numpy()
            temp = path.with_name(path.name + ".oi-quantity.tmp")
            try:
                replacement.to_parquet(temp, index=False)
                verify = pd.read_parquet(temp)
                if not verify["oi_qty"].equals(replacement["oi_qty"]):
                    raise ValueError(f"quantity write verification failed: {path}")
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            upgraded.append(path.name)
        return upgraded
