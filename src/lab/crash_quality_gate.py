"""Read-only pre-holdout crash market-data and signal-eligibility diagnostics.

This is a quality gate, not a trading experiment. It never calculates
strategy P&L, selects a trading threshold, records a trial, or opens
protected holdout archives. Reported windows are *potential* decision
times, not trading signals or an estimate of missed profitability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from lab import crash_data, crash_study
from lab.config import load_config
from lab.crash_oi_migration import _holdout_boundary
from lab.market_symbols import SYMBOL_RE


def _gaps(missing: pd.DatetimeIndex, *, limit: int = 12) -> list[dict]:
    """Summarize contiguous minute gaps; only emit a bounded diagnostic."""
    if missing.empty:
        return []
    values = missing.sort_values()
    result = []
    beginning = last = values[0]
    n = 1
    for stamp in values[1:]:
        if stamp - last == pd.Timedelta(minutes=1):
            last = stamp
            n += 1
        else:
            result.append({
                "start": beginning.isoformat(), "end": last.isoformat(), "minutes": n,
            })
            beginning = last = stamp
            n = 1
            if len(result) >= limit:
                return result
    if len(result) < limit:
        result.append({
            "start": beginning.isoformat(), "end": last.isoformat(), "minutes": n,
        })
    return result


def _validate_klines(frame: pd.DataFrame, boundary: pd.Timestamp) -> pd.DatetimeIndex:
    needed = {"ts", "open", "high", "low", "close", "quote_volume"}
    if not needed <= set(frame):
        raise ValueError(f"missing candle columns: {sorted(needed - set(frame))}")
    ts = frame["ts"]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype):
        raise ValueError("candle timestamps must be timezone-aware")
    if ts.dt.tz is None or ts.dt.tz.utcoffset(None) != pd.Timedelta(0):
        # tz.utcoffset(None) is None for some fixed-offset timezone
        # implementations; absolute nanosecond timestamps are used below.
        if str(ts.dt.tz) not in ("UTC", "datetime.timezone.utc"):
            raise ValueError("candle timestamps must be UTC")
    stamps = pd.DatetimeIndex(ts)
    if stamps.empty or not stamps.is_monotonic_increasing or stamps.has_duplicates:
        raise ValueError("candle timestamps must be nonempty, sorted, unique")
    if (stamps >= boundary).any():
        raise ValueError("pre-holdout candle frame contains protected timestamp")
    if np.any(stamps.asi8 % pd.Timedelta(minutes=1).value):
        raise ValueError("off-grid one-minute candle timestamp")
    numeric = frame[["open", "high", "low", "close", "quote_volume"]].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError("nonfinite candle prices or quote volume")
    o, h, l, c, v = numeric.T
    if (
        (o <= 0).any() or (h <= 0).any() or (l <= 0).any()
        or (c <= 0).any() or (v < 0).any()
        or (h < np.maximum(o, c)).any()
        or (l > np.minimum(o, c)).any()
        or (l > h).any()
    ):
        raise ValueError("invalid candle OHLC or quote-volume constraints")
    return stamps


def _validate_source_paths(root: Path, symbol: str) -> None:
    """Reject symlinked cache sources (including aliases to protected data)."""
    for kind in ("klines", "oi"):
        folder = root / kind / symbol
        if folder.is_symlink():
            raise ValueError(f"unsafe symlink cache folder: {folder}")
        if folder.exists():
            for path in folder.glob("*.parquet"):
                if path.is_symlink():
                    raise ValueError(f"unsafe symlink cache file: {path}")


def audit_quality(
    config_path: Path, symbol: str = "BTCUSDT", *, window_min: int = 60,
) -> dict:
    """Evaluate potential one-minute decision windows on pre-holdout data."""
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be an uppercase perpetual symbol")
    if not isinstance(window_min, int) or not 1 <= window_min <= 240:
        raise ValueError("window_min must be an integer between 1 and 240")
    cfg = load_config(config_path)
    boundary = pd.Timestamp(_holdout_boundary(cfg), tz="UTC")
    root = cfg.data_dir / "crash"
    _validate_source_paths(root, symbol)

    # The loader filters filenames *before* opening any archive.
    k = crash_data.load(root, "klines", symbol, before=boundary)
    o = crash_data.load(root, "oi", symbol, before=boundary)
    if k is None or o is None or not len(k) or not len(o):
        raise ValueError("qualified input requires pre-holdout candles and open interest")
    kt = _validate_klines(k, boundary)
    if (o["ts"] >= boundary).any():
        raise ValueError("pre-holdout OI frame contains protected timestamp")
    # The OI loader has already enforced per-row quantity quality invariants.
    first = kt[0]
    last = kt[-1]
    grid = pd.date_range(first, last, freq="min")
    absent_candles = grid.difference(kt)

    b = crash_study.bars(k, o)
    w = window_min
    bad_absent = b["oi_absent_source"].rolling(w + 1, min_periods=1).max().astype(bool)
    bad_quarantine = b["oi_quarantined_source"].rolling(w + 1, min_periods=1).max().astype(bool)
    banned = bad_absent | bad_quarantine

    # Do not calculate a price-drop/volume strategy or any P&L here.
    # These are potential decision times where necessary inputs are
    # observable; they are independent of any strategy thresholds.
    close = b["close"]
    quote = b["quote_volume"]
    price_ready = close.notna() & close.shift(w).notna()
    # The crash detector uses a current-window sum and a seven-day
    # pre-window median, each requiring at least 1440 observations.
    vol_current = quote.rolling(w, min_periods=w).count().eq(w)
    vol_baseline = quote.rolling(
        crash_study.BASE_DAYS * 1440, min_periods=1440
    ).count().shift(w).ge(1440)
    oi_ready = b["oi"].notna() & b["oi"].shift(w).notna()
    candidate = price_ready & vol_current & vol_baseline & oi_ready
    dropped = candidate & banned

    quarantined_rows = int((~o["oi_valid"].to_numpy(dtype=bool)).sum()) if "oi_valid" in o else 0
    # A delayed source event can fall outside the loaded candle range;
    # distinguish archive-wide observations from publication events in b.
    return {
        "read_only": True,
        "scope": "pre_holdout_data_quality_only",
        "holdout_start": boundary.isoformat(),
        "symbol": symbol,
        "window_min": w,
        "klines": {
            "loaded_rows": len(k),
            "grid_minutes": len(grid),
            "missing_minutes": len(absent_candles),
            "missing_ranges_first_12": _gaps(absent_candles),
            "first": first.isoformat(),
            "last": last.isoformat(),
            "trailing_to_boundary_minutes": max(
                0, int((boundary - last) / pd.Timedelta(minutes=1)) - 1
            ),
        },
        "oi": {
            "loaded_rows": len(o),
            "quarantined_source_rows": quarantined_rows,
            "missing_source_events_within_candle_grid": int(b["oi_absent_source"].sum()),
            "quarantined_events_within_candle_grid": int(b["oi_quarantined_source"].sum()),
        },
        "potential_decision_windows": {
            "data_ready_before_quality_veto": int(candidate.sum()),
            "excluded_by_any_oi_quality_veto": int(dropped.sum()),
            "excluded_with_missing_source_in_lookback": int((candidate & bad_absent).sum()),
            "excluded_with_quarantine_in_lookback": int((candidate & bad_quarantine).sum()),
            "excluded_with_both_reasons": int((candidate & bad_absent & bad_quarantine).sum()),
            "data_ready_after_quality_veto": int((candidate & ~banned).sum()),
            "all_minutes_under_oi_quality_veto": int(banned.sum()),
        },
        "assumed_taker_round_trip_cost_bps": {
            "fee_5_slippage_5_each_side": 20,
            "fee_5_slippage_10_each_side": 30,
            "fee_5_slippage_20_each_side": 50,
        },
        "qualification_limits": [
            "Window counts are data eligibility, not detected crashes or trade counts",
            "Cost scenarios are assumptions, not measured fills, funding, or P&L",
            "Source gaps are reported, not interpolated or repaired",
            "Full archived input manifest and statistical trial qualification remain separate",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read-only pre-holdout crash candle/OI eligibility gate; no trading P&L"
    )
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--window-min", type=int, default=60)
    args = p.parse_args(argv)
    try:
        result = audit_quality(args.config, args.symbol, window_min=args.window_min)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        p.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
