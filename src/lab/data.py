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
"""

from __future__ import annotations

import json
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


def validate(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalize and check a daily OHLCV frame. Returns a clean copy or raises DataError."""
    if df is None or len(df) == 0:
        raise DataError(f"{ticker}: no rows")
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    missing = [c for c in COLUMNS if c not in out.columns]
    if missing:
        raise DataError(f"{ticker}: missing columns {missing}")
    out = out[COLUMNS]

    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "date"
    out = out.sort_index()
    if out.index.has_duplicates:
        dups = out.index[out.index.duplicated()].unique()[:3]
        raise DataError(f"{ticker}: duplicate dates, e.g. {[str(d.date()) for d in dups]}")

    out = out.apply(pd.to_numeric, errors="coerce")
    # Never convert unknown volume to zero or silently delete missing price
    # sessions. Either operation changes the inference problem and may conceal
    # market outages, delisting events or vendor errors.
    valid_rows = np.isfinite(out.to_numpy(dtype=float)).all(axis=1)
    if not valid_rows.all():
        bad = out.index[np.flatnonzero(~valid_rows)[0]]
        raise DataError(f"{ticker}: missing/nonfinite OHLCV on {bad.date()} (quarantine, do not impute)")
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
    out["high"] = np.maximum(out["high"], body_hi)
    out["low"] = np.minimum(out["low"], body_lo)
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
    clean = validate(df, ticker)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    clean.to_parquet(cfg.data_dir / f"{ticker}.parquet")
    entry = {
        "source": source,
        "adjusted": adjusted,
        "survivorship_free": survivorship_free,
        "note": note,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "first": str(clean.index[0].date()),
        "last": str(clean.index[-1].date()),
        "rows": len(clean),
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


def load_many_unmodified(cfg: LabConfig, tickers) -> dict[str, pd.DataFrame]:
    """Return the stored Parquet rows unchanged for strict as-of diagnostics.

    The saved files were already normalized at ingestion. This is *not* vendor
    raw-data recovery, source-finality verification or a holdout access boundary.
    """
    result = {}
    for ticker in tickers:
        p = cfg.data_dir / f"{ticker.upper()}.parquet"
        if not p.exists():
            raise DataError(f"no stored price history for {ticker}")
        result[ticker] = pd.read_parquet(p)
    return result


def quality(cfg: LabConfig, tickers) -> dict:
    """Summary of how trustworthy the data behind a result is."""
    m = manifest(cfg)
    entries = [m.get(t, {}) for t in tickers]
    sources = sorted({e.get("source", "?") for e in entries})
    pit = membership(cfg) is not None
    return {
        "sources": sources,
        "adjusted": all(e.get("adjusted") for e in entries),
        "survivorship_free": all(e.get("survivorship_free") for e in entries) and pit,
        "point_in_time_universe": pit,
    }


def quality_warning(q: dict) -> str | None:
    issues = []
    if not q["survivorship_free"]:
        issues.append(
            "survivorship bias: the universe only contains stocks that survived, so results are "
            "biased upward" + ("" if q["point_in_time_universe"] else " (no point-in-time membership file)")
        )
    if not q["adjusted"]:
        issues.append("unadjusted prices: splits and dividends appear as price jumps")
    return "; ".join(issues) or None


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
            actions=False,
            raise_errors=True,
        )
    except Exception as e:  # yfinance raises a variety of types
        raise DataError(f"{ticker}: Yahoo Finance download failed: {e}") from e
    if df is None or df.empty:
        raise DataError(f"{ticker}: Yahoo Finance returned no data (check the symbol)")
    return df.rename(columns=str.lower)[COLUMNS]


def import_csv(path: str | Path, date_column: str | None = None) -> pd.DataFrame:
    """CSV with a date column and open/high/low/close/volume (any case). Exports from
    Databento, Interactive Brokers, Stooq or a spreadsheet all work."""
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    dc = date_column or next(
        (cols[c] for c in ("date", "datetime", "timestamp", "time", "ts_event") if c in cols), None
    )
    if dc is None:
        raise DataError(f"{path}: no date column found (pass --date-column)")
    df.index = pd.to_datetime(df.pop(dc), utc=False)
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


def block_bootstrap_panel(frames: dict[str, pd.DataFrame], seed: int, block: int = 20) -> dict[str, pd.DataFrame]:
    """Joint calendar-block bootstrap with preserved instrument availability.

    Draw *one* source-calendar interval for every destination interval. A source
    block is eligible only when it contains all observations required by the
    destination block. Missing dates remain missing; no late-listing history or
    post-delisting price is invented. All concurrently observed assets receive
    shocks from the *same* sampled source market date.

    This conditions the null on the observed availability mask. Sparse panels
    may have very few eligible source blocks: report that limitation when
    interpreting statistical significance; no historical market is generated.
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
    global_pick = np.empty(n, dtype=np.int64)
    for start in range(0, n, block):
        width = min(block, n - start)
        eligible = np.ones(n - width + 1, dtype=bool)
        # Masks are indexed on the UNION calendar, not row numbers within
        # each ticker. Every target observation must have a source observation.
        for mask in masks.values():
            for offset in np.flatnonzero(mask[start : start + width]):
                eligible &= mask[offset : offset + n - width + 1]
        options = np.flatnonzero(eligible)
        if not len(options):
            raise DataError(f"no joint source block for union-calendar interval {start}:{start + width}")
        donor_start = int(options[rng.integers(len(options))])
        global_pick[start : start + width] = np.arange(donor_start, donor_start + width)

    out = {}
    for t, df in ordered.items():
        mask = masks[t]
        donor_dates = calendar[global_pick[mask]]
        donor = df.loc[donor_dates]
        opn, high, low, close, volume = (df[k].to_numpy(dtype=float) for k in COLUMNS)
        previous_close = np.concatenate([[opn[0]], close[:-1]])
        gap, day_move = opn / previous_close, close / opn
        excursion_up = high / np.maximum(opn, close)
        excursion_down = low / np.minimum(opn, close)
        # Translate shared calendar donors into asset-specific source row
        # indices without forward/backward filling non-trading sessions.
        donor_rows = df.index.get_indexer(donor.index)
        if (donor_rows < 0).any():
            raise DataError(f"{t}: donor calendar lost a required instrument observation")
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
    return out
