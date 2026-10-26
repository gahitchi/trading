"""
Market data: fetch, import, validate and store daily OHLCV bars.

Storage is one Parquet file per ticker plus manifest.json recording where each
series came from (source, whether prices are split/dividend adjusted, when it
was fetched). Every backtest records which manifest entries it used.

Also provides the two NULL MODELS used by the noise-floor test:
  - gbm():             geometric random walk, no pattern by construction
  - block_bootstrap(): real daily returns reshuffled in blocks. Keeps the real
                       distribution (fat tails, short-term volatility clustering)
                       but destroys longer-range predictability. A strategy that
                       can't beat these samples has not shown an edge.
  - block_bootstrap_panel(): the same for a whole universe, with one shared draw of
                       market dates so the stocks keep their co-movement; a stock
                       missing at a shared date falls back to its own history
                       for that block, and the share of such blocks is reported
                       (ChatGPT, G002).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from lab.config import LabConfig

COLUMNS = ["open", "high", "low", "close", "volume"]
# Adjusted data can carry tiny high/low inconsistencies from rounding; repair those,
# reject anything bigger as corrupt.
_REPAIR_TOLERANCE = 0.01


class DataError(Exception):
    pass


# --------------------------------------------------------------------------- validation


def validate(df: pd.DataFrame, ticker: str, *, drop_incomplete: bool = False, keep_time: bool = False) -> pd.DataFrame:
    """Normalize and check a daily OHLCV frame. Returns a clean copy or raises DataError.

    Non-finite prices or volumes are always an error. A bar missing an open, high, low or close is an
    error too, unless drop_incomplete (vendor imports only, see save()): then it is dropped and its date
    recorded in out.attrs["dropped_incomplete"], which save() writes to the manifest, so the gap is never
    silent (G007-F10). Missing sessions stay missing; no bar is ever invented.
    keep_time: intraday bars (lab.intraday): the time of day is kept instead of normalizing to the date."""
    if df is None or len(df) == 0:
        raise DataError(f"{ticker}: no rows")
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    missing = [c for c in COLUMNS if c not in out.columns]
    if missing:
        raise DataError(f"{ticker}: missing columns {missing}")
    out = out[COLUMNS]

    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.hasnans:
        raise DataError(f"{ticker}: missing timestamp")
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    out.index = idx if keep_time else idx.normalize()
    out.index.name = "start" if keep_time else "date"
    out = out.sort_index()
    if out.index.has_duplicates:
        dups = out.index[out.index.duplicated()].unique()[:3]
        raise DataError(f"{ticker}: duplicate bars, e.g. {[str(d) for d in dups]}")

    out = out.apply(pd.to_numeric, errors="coerce")
    ohlc = ["open", "high", "low", "close"]
    infinite = np.isinf(out[[*ohlc, "volume"]].to_numpy(dtype=float)).any(axis=1)
    if infinite.any():
        raise DataError(f"{ticker}: non-finite value on {out.index[infinite][0].date()}")
    if out["volume"].isna().any():
        raise DataError(f"{ticker}: missing/nonfinite volume; liquidity cannot be inferred")
    incomplete = out[ohlc].isna().any(axis=1)
    dropped = [str(d) if keep_time else str(d.date()) for d in out.index[incomplete]]
    if dropped and not drop_incomplete:
        raise DataError(f"{ticker}: {len(dropped)} incomplete bar(s) (missing open/high/low/close), e.g. {dropped[:5]}")
    out = out[~incomplete]
    if len(out) == 0:
        raise DataError(f"{ticker}: no complete OHLC rows")
    prices = out[["open", "high", "low", "close"]]
    if (prices <= 0).any().any():
        bad = out.index[(prices <= 0).any(axis=1)][0]
        raise DataError(f"{ticker}: non-positive price on {bad.date()}")
    if (out["volume"] < 0).any():
        raise DataError(f"{ticker}: negative volume")

    body_hi = out[["open", "close"]].max(axis=1)
    body_lo = out[["open", "close"]].min(axis=1)
    hi_gap = (body_hi - out["high"]) / body_hi
    lo_gap = (out["low"] - body_lo) / body_lo
    worst = max(hi_gap.max(), lo_gap.max())
    if worst > _REPAIR_TOLERANCE:
        d = out.index[(hi_gap > _REPAIR_TOLERANCE) | (lo_gap > _REPAIR_TOLERANCE)][0]
        raise DataError(f"{ticker}: high/low inconsistent with open/close on {d.date()} ({worst:.2%})")
    repairs = [str(d) for d in out.index[(hi_gap > 0) | (lo_gap > 0)]]
    out.attrs["repaired_high_low"] = repairs
    out["high"] = np.maximum(out["high"], body_hi)
    out["low"] = np.minimum(out["low"], body_lo)
    out.attrs["dropped_incomplete"] = dropped
    return out


# --------------------------------------------------------------------------- storage


def _manifest_path(cfg: LabConfig) -> Path:
    return cfg.data_dir / "manifest.json"


def manifest(cfg: LabConfig) -> dict:
    p = _manifest_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {}


def save(
    cfg: LabConfig,
    ticker: str,
    df: pd.DataFrame,
    *,
    source: str,
    adjusted: bool,
    survivorship_free: bool = False,
    note: str = "",
) -> dict:
    """Store a validated series with its provenance.

    adjusted:           prices are back-adjusted for splits and dividends
    survivorship_free:  the dataset this came from also contains stocks that were later
                        delisted (so a universe built from it is not limited to survivors)
    """
    ticker = ticker.upper()
    clean = validate(df, ticker, drop_incomplete=True)
    dropped = clean.attrs.get("dropped_incomplete", [])
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    clean.to_parquet(cfg.data_dir / f"{ticker}.parquet")
    entry = {
        "source": source,
        "adjusted": adjusted,
        **({"splits": df.attrs["splits"]} if "splits" in df.attrs else {}),
        "survivorship_free": survivorship_free,
        "note": note,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "first": str(clean.index[0].date()),
        "last": str(clean.index[-1].date()),
        "rows": len(clean),
        "dropped_incomplete": dropped,
        "repaired_high_low": clean.attrs.get(
            "repaired_high_low", []
        ),  # vendor bars missing a price, removed at import (never silently)
    }
    m = manifest(cfg)
    m[ticker] = entry
    _manifest_path(cfg).write_text(json.dumps(m, indent=2, sort_keys=True))
    return entry


def load(cfg: LabConfig, ticker: str) -> pd.DataFrame:
    p = cfg.data_dir / f"{ticker.upper()}.parquet"
    if not p.exists():
        raise DataError(f"no data for {ticker}; run: lab data fetch {ticker}")
    return validate(pd.read_parquet(p), ticker)


def load_many(cfg: LabConfig, tickers) -> dict[str, pd.DataFrame]:
    return {t: load(cfg, t) for t in tickers}


def quality(cfg: LabConfig, tickers) -> dict:
    """Summary of how trustworthy the data behind a result is."""
    m = manifest(cfg)
    entries = [m.get(t, {}) for t in tickers]
    sources = sorted({e.get("source", "?") for e in entries})
    pit = membership(cfg) is not None
    shorts = cfg.risk.max_short_gross > 0
    levered = cfg.risk.max_gross > 1 or cfg.portfolio.leverage > 1 or cfg.portfolio.max_leverage > 1
    return {
        "sources": sources,
        "dropped_incomplete": sum(len(e.get("dropped_incomplete", [])) for e in entries),
        "adjusted": all(e.get("adjusted") for e in entries),
        "survivorship_free": all(e.get("survivorship_free") for e in entries) and pit,
        "point_in_time_universe": pit,
        "shorting": shorts,
        "leverage": levered,
        "borrow_model": "historical_close_book" if cfg.borrow.history is not None else "assumed",
        "assumed_borrow_apr": cfg.margin.borrow_apr if shorts and cfg.borrow.history is None else None,
        "assumed_debit_apr": cfg.margin.debit_apr if (shorts or levered) else None,
        "assumed_credit_apr": cfg.margin.credit_apr or None,
        "assumed_short_rebate_apr": (cfg.margin.short_rebate_apr or None) if shorts else None,
        "split_history": all("splits" in e for e in entries) if entries else False,
        "delistings": {t: d for t, d in (delistings(cfg) or {}).items() if t in set(tickers)},
        "risk_free": "data/risk_free.csv" if risk_free_path(cfg).exists() else None,
    }


def quality_warning(q: dict) -> str | None:
    issues = []
    if not q["survivorship_free"]:
        issues.append(
            "survivorship bias: the universe only contains stocks that survived, so results are "
            "biased upward" + ("" if q["point_in_time_universe"] else " (no point-in-time membership file)")
        )
    if q.get("dropped_incomplete"):
        issues.append(f"{q['dropped_incomplete']} vendor bar(s) missing a price were removed at import (see manifest)")
    if not q["adjusted"]:
        issues.append("unadjusted prices: splits and dividends appear as price jumps")
        if q.get("shorting"):
            issues.append("shorts on unadjusted prices profit from every dividend drop: use adjusted data")
    if q.get("shorting") and q.get("borrow_model") == "historical_close_book":
        issues.append(
            "historical borrow snapshots gate new shorts and supply per-day fees using prior closing books; "
            "not a live locate, intraday inventory, recall/liquidation model or verified vendor provenance"
        )
    elif q.get("shorting"):
        issues.append(
            f"borrow fees ASSUMED at {q['assumed_borrow_apr']:.2%}/yr for every stock (no historical borrow data; "
            "hard-to-borrow stocks cost far more)"
        )
    if q.get("assumed_debit_apr") is not None:
        issues.append(f"interest on borrowed cash ASSUMED at {q['assumed_debit_apr']:.2%}/yr")
    if q.get("assumed_credit_apr"):
        issues.append(f"interest on free cash ASSUMED at {q['assumed_credit_apr']:.2%}/yr")
    if q.get("assumed_short_rebate_apr"):
        issues.append(f"rebate on short proceeds ASSUMED at {q['assumed_short_rebate_apr']:.2%}/yr")
    if q.get("adjusted") and not q.get("split_history", True):
        issues.append(
            "split history unknown for some adjusted series: per-share commissions on pre-split dates are charged "
            "on adjusted share counts and overstated (refetch with `lab data fetch` to record splits)"
        )
    for ticker, d in (q.get("delistings") or {}).items():
        if d.get("assumed"):
            issues.append(f"{ticker} delisting recovery ASSUMED {d['delist_return']:.0%} (no vendor value; not a tradable fill)")
    return "; ".join(issues) or None


# --------------------------------------------------------------------------- delistings

DELISTING_COLUMNS = ("ticker", "delist_date", "delist_type", "delist_return", "settlement_price", "source")
# Shumway (1997): performance-related delistings without a recorded value lose about 30% on average.
ASSUMED_DISTRESS_RETURN = -0.30
DISTRESS_TYPES = frozenset({"bankruptcy", "distress", "performance", "liquidation"})


def delistings_path(cfg: LabConfig) -> Path:
    return cfg.data_dir / "delistings.csv"


def save_delistings(cfg: LabConfig, path: str | Path) -> pd.DataFrame:
    """CSV of ticker, delist_date, delist_type, delist_return, settlement_price, source (Gemini audit F-R09).

    Every row needs its vendor (`source`). A recorded settlement_price (or last close x
    (1 + delist_return)) is recovery *metadata*, not a tradable quote. For distress with no
    value, an ASSUMED -30% research scenario (Shumway, 1997) is recorded and warned
    about; no simulated market settlement is authorized. Other delisting types need a value."""
    df = pd.read_csv(path, dtype=str).rename(columns=str.lower).fillna("")
    missing = set(DELISTING_COLUMNS) - set(df.columns)
    if missing:
        raise DataError(f"{path}: missing columns {sorted(missing)}")
    df = df[list(DELISTING_COLUMNS)].apply(lambda col: col.str.strip())
    df["ticker"] = df["ticker"].str.upper()
    df["delist_type"] = df["delist_type"].str.lower()
    for i, r in df.iterrows():
        where = f"{path}: row {i + 2} ({r['ticker'] or '?'})"
        if not r["ticker"] or not r["source"]:
            raise DataError(f"{where}: ticker and source (the vendor) are required")
        try:
            date.fromisoformat(r["delist_date"])
            ret = float(r["delist_return"]) if r["delist_return"] else None
            price = float(r["settlement_price"]) if r["settlement_price"] else None
        except ValueError as e:
            raise DataError(f"{where}: {e}") from e
        if ret is not None and (not math.isfinite(ret) or not -1 <= ret <= 10):
            raise DataError(f"{where}: delist_return must be finite and between -1 and 10")
        if price is not None and (not math.isfinite(price) or price < 0):
            raise DataError(f"{where}: settlement_price must be finite and nonnegative")
        if ret is None and price is None and r["delist_type"] not in DISTRESS_TYPES:
            raise DataError(
                f"{where}: a {r['delist_type'] or 'untyped'} delisting needs delist_return or settlement_price"
            )
    if df["ticker"].duplicated().any():
        raise DataError(f"{path}: one delisting per ticker")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(delistings_path(cfg), index=False)
    return df


def delistings(cfg: LabConfig) -> dict[str, dict] | None:
    """Read persisted settlement metadata; reject malformed/tampered records.

    Validation on initial import is insufficient because the stored CSV can
    change after it has been approved for research.
    """
    p = delistings_path(cfg)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, dtype=str).fillna("")
    except (OSError, ValueError) as exc:
        raise DataError(f"{p}: invalid delisting evidence") from exc
    missing = set(DELISTING_COLUMNS) - set(df.columns)
    if missing:
        raise DataError(f"{p}: missing columns {sorted(missing)}")
    out = {}
    for i, r in df.iterrows():
        ticker = str(r["ticker"]).strip().upper()
        where = f"{p}: row {i + 2} ({ticker or '?'})"
        if not ticker or not str(r["source"]).strip():
            raise DataError(f"{where}: ticker and source are required")
        if ticker in out:
            raise DataError(f"{where}: one delisting per ticker")
        try:
            when = date.fromisoformat(str(r["delist_date"]))
            has_return = bool(str(r["delist_return"]).strip())
            has_price = bool(str(r["settlement_price"]).strip())
            ret = float(r["delist_return"]) if has_return else None
            price = float(r["settlement_price"]) if has_price else None
        except (TypeError, ValueError, OverflowError) as exc:
            raise DataError(f"{where}: malformed delisting date or recovery value") from exc
        if ret is not None and (not math.isfinite(ret) or not -1 <= ret <= 10):
            raise DataError(f"{where}: delist_return must be finite and between -1 and 10")
        if price is not None and (not math.isfinite(price) or price < 0):
            raise DataError(f"{where}: settlement_price must be finite and nonnegative")
        kind = str(r["delist_type"]).strip().lower()
        assumed = ret is None and price is None
        if assumed and kind not in DISTRESS_TYPES:
            raise DataError(f"{where}: {kind or 'untyped'} delisting needs a recovery value")
        out[ticker] = {
            "delist_date": when,
            "delist_type": kind,
            "delist_return": ASSUMED_DISTRESS_RETURN if assumed else (ret if ret is not None else float("nan")),
            "settlement_price": price,
            "source": str(r["source"]).strip(),
            "assumed": assumed,
        }
    return out


# --------------------------------------------------------------------------- point-in-time universe


def membership_path(cfg: LabConfig) -> Path:
    return cfg.data_dir / "membership.csv"


def save_membership(cfg: LabConfig, path: str | Path) -> pd.DataFrame:
    """CSV with columns ticker, start, end (end empty = still a member). One row per
    membership spell; a ticker may have several rows."""
    df = pd.read_csv(path, dtype=str).rename(columns=str.lower)
    missing = {"ticker", "start"} - set(df.columns)
    if missing:
        raise DataError(f"{path}: missing columns {sorted(missing)}")
    if "end" not in df:
        df["end"] = ""
    df = df[["ticker", "start", "end"]].fillna("")
    df["ticker"] = df["ticker"].str.strip().str.upper()
    for col in ("start", "end"):
        bad = df[col][(df[col] != "") & pd.to_datetime(df[col], errors="coerce").isna()]
        if len(bad):
            raise DataError(f"{path}: invalid {col} date {bad.iloc[0]!r}")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(membership_path(cfg), index=False)
    return df


def membership(cfg: LabConfig) -> dict[str, list[tuple[date, date | None]]] | None:
    p = membership_path(cfg)
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype=str).fillna("")
    out: dict[str, list] = {}
    for _, r in df.iterrows():
        out.setdefault(r["ticker"], []).append(
            (date.fromisoformat(r["start"]), date.fromisoformat(r["end"]) if r["end"] else None)
        )
    return out


def risk_free_path(cfg: LabConfig) -> Path:
    return cfg.data_dir / "risk_free.csv"


def save_risk_free(cfg: LabConfig, df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Store a daily risk-free rate series: columns date and rate, the annual rate in PERCENT (FRED's DTB3
    convention, 5.25 = 5.25%). Missing values (holidays) are dropped, not filled."""
    df = df.rename(columns=str.lower)
    if not {"date", "rate"} <= set(df.columns):
        raise DataError("risk-free rates need columns date and rate (annual percent)")
    out = pd.DataFrame(
        {"date": pd.to_datetime(df["date"], errors="coerce"), "rate": pd.to_numeric(df["rate"], errors="coerce")}
    ).dropna()
    if out.empty:
        raise DataError("risk-free rate file has no valid rows")
    if not out["rate"].between(-5, 30).all():
        raise DataError("risk-free rates must be annual percentages between -5 and 30")
    out = out.drop_duplicates("date").sort_values("date")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    out.assign(date=out["date"].dt.date.astype(str), source=source).to_csv(risk_free_path(cfg), index=False)
    return out


def risk_free(cfg: LabConfig) -> pd.Series | None:
    """Annual risk-free rate (fraction) by date, point in time: a day's published rate is used from the next
    day on, never on the day itself. None without data/risk_free.csv (metrics then assume 0%)."""
    p = risk_free_path(cfg)
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["date"])
    s = pd.Series(df["rate"].to_numpy(dtype=float) / 100, index=pd.DatetimeIndex(df["date"]))
    s.index = s.index + pd.Timedelta(days=1)
    return s.sort_index()


def fetch_risk_free() -> pd.DataFrame:
    """3-month Treasury bill secondary-market rate (FRED series DTB3, free, no account)."""
    import io
    import urllib.request

    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = pd.read_csv(io.BytesIO(response.read()))
    except Exception as e:
        raise DataError(f"FRED download failed: {e}") from e
    raw.columns = ["date", "rate"]
    return raw


# --------------------------------------------------------------------------- sources


def fetch_yfinance(ticker: str, start: date, end: date | None = None) -> pd.DataFrame:
    """Daily bars from Yahoo Finance, split- and dividend-adjusted (free, no account).

    Yahoo's data is unofficial: good for research, not guaranteed. Prices are
    back-adjusted, so a stock's whole history changes slightly after each dividend.
    """
    import yfinance as yf

    try:
        df = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=end.isoformat() if end else None,
            interval="1d",
            auto_adjust=True,
            actions=True,
            raise_errors=True,
        )
    except Exception as e:  # yfinance raises a variety of types
        raise DataError(f"{ticker}: Yahoo Finance download failed: {e}") from e
    if df is None or df.empty:
        raise DataError(f"{ticker}: Yahoo Finance returned no data (check the symbol)")
    df = df.rename(columns=str.lower)
    out = df[COLUMNS]
    # The split history (date, ratio) lets per-share costs use the real share count (Gemini audit F-R07).
    ratios = df["stock splits"] if "stock splits" in df else pd.Series(dtype=float)
    out.attrs["splits"] = [
        [str(pd.Timestamp(d).date()), float(r)] for d, r in ratios.items() if math.isfinite(r) and r > 0 and r != 1
    ]
    return out


def import_csv(path: str | Path, date_column: str | None = None, *, keep_time: bool = False) -> pd.DataFrame:
    """CSV with a date column and open/high/low/close/volume (any case). Exports from
    Databento, Interactive Brokers, Stooq or a spreadsheet all work. With keep_time (intraday bars), timestamps
    with a UTC offset are converted to New York time; naive ones are taken as New York time already."""
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    dc = date_column or next(
        (cols[c] for c in ("date", "datetime", "timestamp", "time", "ts_event") if c in cols), None
    )
    if dc is None:
        raise DataError(f"{path}: no date column found (pass --date-column)")
    raw = df.pop(dc).astype(str)
    if keep_time and raw.str.contains(r"(?:[+-]\d\d:?\d\d|Z)$").any():
        idx = pd.DatetimeIndex(pd.to_datetime(raw, utc=True)).tz_convert("America/New_York").tz_localize(None)
    else:
        idx = pd.DatetimeIndex(pd.to_datetime(raw, utc=False))
    df.index = idx
    return df


def sample_goog() -> pd.DataFrame:
    """Real GOOG daily prices 2004-08-19 .. 2013-03-01 (unadjusted), shipped with the
    `backtesting` package. Used to verify the pipeline offline on genuine market data."""
    try:
        from backtesting.test import GOOG
    except ImportError as e:
        raise DataError("install the dev extras for the offline sample: pip install -e '.[dev]'") from e
    return GOOG.rename(columns=str.lower)[COLUMNS]


# --------------------------------------------------------------------------- null models


def gbm(
    n_days: int,
    seed: int,
    *,
    start: str = "2010-01-04",
    price: float = 100.0,
    ann_vol: float = 0.25,
    ann_drift: float = 0.0,
) -> pd.DataFrame:
    """Random-walk daily bars (business days). No exploitable pattern by construction."""
    rng = np.random.default_rng(seed)
    dt = 1 / 252
    steps = 8  # intraday sub-steps for realistic highs/lows
    lr = (ann_drift - 0.5 * ann_vol**2) * dt / steps + ann_vol * np.sqrt(dt / steps) * rng.standard_normal(
        n_days * steps
    )
    path = price * np.exp(np.concatenate([[0.0], np.cumsum(lr)]))
    seg = path[1:].reshape(n_days, steps)
    opens = path[:-1].reshape(n_days, steps)[:, 0]
    close = seg[:, -1]
    high = np.maximum(seg.max(axis=1), opens)
    low = np.minimum(seg.min(axis=1), opens)
    vol = rng.lognormal(14, 0.4, n_days).round()
    idx = pd.bdate_range(start, periods=n_days)
    return pd.DataFrame({"open": opens, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def block_bootstrap(df: pd.DataFrame, seed: int, block: int = 20) -> pd.DataFrame:
    """Rebuild a price path from the real series' own days, reshuffled in blocks.

    Each day is reduced to scale-free pieces (overnight gap, open-to-close move,
    high/low excursions, volume) and whole blocks of consecutive days are drawn
    with replacement. The dates are kept, so warm-up/train/test windows still apply.
    """
    d = validate(df, "bootstrap")
    o, h, lo, c, v = (d[k].to_numpy() for k in COLUMNS)
    prev_c = np.concatenate([[o[0]], c[:-1]])
    gap = o / prev_c
    oc = c / o
    up = h / np.maximum(o, c)
    dn = lo / np.minimum(o, c)
    n = len(d)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, max(1, n - block), size=n // block + 1)
    pick = np.concatenate([np.arange(s, min(s + block, n)) for s in starts])[:n]

    out_o = np.empty(n)
    out_c = np.empty(n)
    last = c[0]
    for i, j in enumerate(pick):
        out_o[i] = last * gap[j]
        out_c[i] = out_o[i] * oc[j]
        last = out_c[i]
    out_h = np.maximum(out_o, out_c) * up[pick]
    out_l = np.minimum(out_o, out_c) * dn[pick]
    return pd.DataFrame({"open": out_o, "high": out_h, "low": out_l, "close": out_c, "volume": v[pick]}, index=d.index)


def block_bootstrap_panel(
    frames: dict[str, pd.DataFrame], seed: int, block: int = 20, *, return_diagnostics: bool = False
) -> dict[str, pd.DataFrame] | tuple[dict[str, pd.DataFrame], dict]:
    """Sample a common market-date path without conditioning it on late listings.

    An instrument uses the common donor only if all required source sessions exist.
    Otherwise *that instrument alone* draws a valid interval from its own history.
    Availability masks are exact: no invented prelisting/postdelisting bars and no
    backfills. Diagnose where fallback sacrifices cross-asset dependence.

    Stationary bootstrap (Politis & Romano, 1994; Gemini audit F-R04): block lengths are geometric with mean
    `block` sessions, so no period of the resample repeats at multiples of a fixed block, and a donor block
    never wraps past the last session. The mean stays a fixed parameter rather than a data-driven choice: a
    length estimated from the panel would change when a stock is added, and an unrelated late listing must
    not change an established stock's null (G002-F2).
    """
    if not frames:
        raise DataError("joint bootstrap requires at least one instrument")
    if type(block) is not int or block < 1:
        raise DataError("joint bootstrap block size must be a positive integer")
    ordered = {t: validate(frames[t], t) for t in sorted(frames)}
    calendar = pd.DatetimeIndex(sorted(set().union(*(set(df.index) for df in ordered.values()))))
    n = len(calendar)
    if n < 2:
        raise DataError("joint bootstrap requires at least two market sessions")
    masks = {t: calendar.isin(df.index) for t, df in ordered.items()}
    rng = np.random.default_rng(seed)

    # CRITICAL: sample this before inspecting ANY ticker mask. Otherwise one
    # newly listed instrument narrows older instruments' return distribution.
    segments = []
    start = 0
    while start < n:
        width = min(int(rng.geometric(1 / block)), n - start)
        segments.append((start, width))
        start += width
    global_pick = np.empty(n, dtype=np.int64)
    for start, width in segments:
        donor_start = int(rng.integers(0, n - width + 1))
        global_pick[start : start + width] = np.arange(donor_start, donor_start + width)

    out = {}
    per_instrument = {}
    fallback_total = participating_total = 0
    for t, df in ordered.items():
        mask = masks[t]
        pick = global_pick.copy()
        fallback_blocks = participating_blocks = 0
        for start, width in segments:
            offsets = np.flatnonzero(mask[start : start + width])
            if not len(offsets):
                continue
            participating_blocks += 1
            # Co-movement is preserved wherever the common donor exists.
            if mask[pick[start + offsets]].all():
                continue
            # Require this asset's *own* available sessions, never the mask
            # of another ticker. The destination interval itself is eligible.
            eligible = np.ones(n - width + 1, dtype=bool)
            for offset in offsets:
                eligible &= mask[offset : offset + n - width + 1]
            options = np.flatnonzero(eligible)
            if not len(options):
                raise DataError(f"{t}: no valid donor for interval {start}:{start + width}")
            donor_start = int(options[rng.integers(len(options))])
            pick[start + offsets] = donor_start + offsets
            fallback_blocks += 1

        donor_dates = calendar[pick[mask]]
        donor_rows = df.index.get_indexer(donor_dates)
        if (donor_rows < 0).any():
            raise DataError(f"{t}: donor calendar lost a required instrument observation")
        opn, high, low, close, volume = (df[k].to_numpy(dtype=float) for k in COLUMNS)
        previous_close = np.concatenate([[opn[0]], close[:-1]])
        gap, day_move = opn / previous_close, close / opn
        excursion_up = high / np.maximum(opn, close)
        excursion_down = low / np.minimum(opn, close)
        rebuilt_open = np.empty(len(df))
        rebuilt_close = np.empty(len(df))
        last = close[0]
        for i, j in enumerate(donor_rows):
            rebuilt_open[i] = last * gap[j]
            rebuilt_close[i] = rebuilt_open[i] * day_move[j]
            last = rebuilt_close[i]
        resampled = pd.DataFrame(
            {
                "open": rebuilt_open,
                "high": np.maximum(rebuilt_open, rebuilt_close) * excursion_up[donor_rows],
                "low": np.minimum(rebuilt_open, rebuilt_close) * excursion_down[donor_rows],
                "close": rebuilt_close,
                "volume": volume[donor_rows],
            },
            index=df.index,
        )
        out[t] = validate(resampled, t)
        fallback_total += fallback_blocks
        participating_total += participating_blocks
        per_instrument[t] = {
            "fallback_blocks": fallback_blocks,
            "participating_blocks": participating_blocks,
            "fallback_fraction": fallback_blocks / participating_blocks if participating_blocks else 0.0,
        }

    diagnostics = {
        "method": "stationary_shared_calendar_with_per_instrument_fallback",
        "mean_block": block,
        "fallback_blocks": fallback_total,
        "participating_blocks": participating_total,
        "fallback_fraction": fallback_total / participating_total if participating_total else 0.0,
        "per_instrument": per_instrument,
    }
    return (out, diagnostics) if return_diagnostics else out
