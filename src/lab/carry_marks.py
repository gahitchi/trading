"""Public hourly perpetual MARK candles, separate from executable trade prices and funding event marks."""

from __future__ import annotations

import json
import os
import re
import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from lab import carry_data as cd
from lab.persistence import file_lock, parse_json
from lab.survey import get_json

MODEL = "hourly-perpetual-mark-ohlc-v1"
METADATA_KEY = b"lab.mark-ohlc-v1"
COLUMNS = ("open", "high", "low", "close")
PAGE_LIMIT = 1000


def _identity(venue, symbol):
    if venue not in cd.VENUES or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+USDT", symbol):
        raise ValueError("mark history requires a supported venue and an uppercase USDT symbol")
    return {
        "model": MODEL,
        "venue": venue,
        "symbol": symbol,
        "interval_ms": cd.HOUR_MS,
        "timestamp": "hour-end",
        "price_kind": "mark",
    }


def path_of(root, venue, symbol):
    _identity(venue, symbol)
    return cd.path_of(root, venue, "perp", "mark-ohlc", symbol)


def _empty():
    return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="ts"), dtype=float)


def _validate(frame):
    if (
        list(frame.columns) != list(COLUMNS)
        or not isinstance(frame.index, pd.DatetimeIndex)
        or str(frame.index.tz) != "UTC"
        or frame.index.hasnans
        or not frame.index.is_unique
        or not frame.index.is_monotonic_increasing
        or not (frame.index == frame.index.floor("h")).all()
    ):
        raise ValueError("invalid mark history: expected unique ordered UTC hour-end OHLC rows")
    if any(not pd.api.types.is_numeric_dtype(t) or pd.api.types.is_bool_dtype(t) for t in frame.dtypes):
        raise ValueError("invalid mark history: OHLC values must be numeric prices")
    if (
        not np.isfinite(frame).all().all()
        or (frame <= 0).any().any()
        or (frame["high"] < frame[["open", "low", "close"]].max(axis=1)).any()
        or (frame["low"] > frame[["open", "high", "close"]].min(axis=1)).any()
    ):
        raise ValueError("invalid mark history: expected finite positive prices with low <= open/close <= high")


def load(root, venue, symbol):
    """One coherent file descriptor for OHLC and identity. Missing files remain empty, never trade-price fallback."""
    path = path_of(root, venue, symbol)
    if not path.exists():
        return _empty()
    with path.open("rb") as source:
        table = pq.read_table(source)
    raw = (table.schema.metadata or {}).get(METADATA_KEY)
    if raw is None or parse_json(raw) != _identity(venue, symbol):
        raise ValueError("missing or conflicting mark history identity")
    if table.column_names != ["ts", *COLUMNS]:
        raise ValueError("invalid mark history schema")
    ts_type = table.schema.field("ts").type
    # Pandas schema metadata can otherwise silently localize a physically naive Arrow timestamp.
    if not pa.types.is_timestamp(ts_type) or ts_type.tz != "UTC":
        raise ValueError("invalid mark history: physical timestamps must be UTC")
    frame = table.to_pandas()
    if list(frame.columns) != ["ts", *COLUMNS]:
        raise ValueError("invalid mark history schema")
    frame = frame.set_index("ts")
    _validate(frame)
    return frame


def download(venue, symbol, since, now, *, get=get_json, pause=lambda: time.sleep(0.5), sleep=time.sleep):
    """Native request bounds name candle OPENS; output names ENDS, with no still-open candle."""
    _identity(venue, symbol)
    since, now = cd._window(since, now)
    start, end = cd._ms(since.ceil("h")), cd._ms(now.floor("h")) - cd.HOUR_MS
    get = cd.patient(get, sleep)
    rows = []
    while start <= end:
        if venue == "binance":
            page = get(
                f"{cd.BINANCE_FAPI}/markPriceKlines?symbol={symbol}&interval=1h"
                f"&startTime={start}&endTime={end}&limit={PAGE_LIMIT}"
            )
        else:
            response = get(
                f"{cd.BYBIT_V5}/mark-price-kline?category=linear&symbol={symbol}&interval=60"
                f"&start={start}&end={end}&limit={PAGE_LIMIT}"
            )
            if not isinstance(response, dict) or response.get("retCode") != 0:
                raise ValueError(
                    f"invalid Bybit mark response: {response.get('retMsg') if isinstance(response, dict) else response}"
                )
            result = response.get("result", {})
            if not isinstance(result, dict) or result.get("symbol") != symbol or result.get("category") != "linear":
                raise ValueError("conflicting Bybit mark response identity")
            page = result.get("list")
        if not isinstance(page, list) or any(
            not isinstance(row, list) or len(row) != (12 if venue == "binance" else 5) for row in page
        ):
            raise ValueError("invalid mark candle page: malformed OHLC rows")
        times = cd._page_times(page, lambda row: row[0], start, end, PAGE_LIMIT, reverse=venue == "bybit", hourly=True)
        try:
            for ts, row in zip(times, page, strict=True):
                if venue == "binance" and (type(row[6]) is not int or row[6] != ts + cd.HOUR_MS - 1):
                    raise ValueError("invalid mark candle close time")
                opening, high, low, close = (cd._value(row[i], positive=True) for i in range(1, 5))
                if high < max(opening, low, close) or low > min(opening, high, close):
                    raise ValueError("expected low <= open/close <= high")
                rows.append([ts + cd.HOUR_MS, opening, high, low, close])
        except (IndexError, KeyError, TypeError, ValueError) as e:
            raise ValueError(f"invalid mark candle: {e}") from e
        if len(page) < PAGE_LIMIT:
            break
        if venue == "binance":
            start = times[-1] + cd.HOUR_MS
        else:
            end = times[-1] - 1
        if start <= end:
            pause()
    if not rows:
        return _empty()
    frame = pd.DataFrame([r[1:] for r in rows], columns=COLUMNS, dtype=float)
    frame.index = pd.DatetimeIndex(pd.to_datetime([r[0] for r in rows], unit="ms", utc=True), name="ts")
    frame = frame.sort_index()
    _validate(frame)
    return frame


def _publish(path, frame, venue, symbol):
    table = pa.Table.from_pandas(frame.reset_index(), preserve_index=False)
    table = table.replace_schema_metadata(
        {**(table.schema.metadata or {}), METADATA_KEY: json.dumps(_identity(venue, symbol), allow_nan=False).encode()}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        pq.write_table(table, tmp)
        with tmp.open("rb") as source:
            os.fsync(source.fileno())
        tmp.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def _grid(since, now):
    return pd.date_range(since.ceil("h") + pd.Timedelta(hours=1), now.floor("h"), freq="h", name="ts")


def update(root, venue, symbol, since, now, **io):
    """Repair every requested missing range, including unknown leading history; commit the WHOLE symbol atomically.

    Empty/partial API answers are not availability proof and stay retryable. No cross-column partial publication.
    """
    since, now = cd._window(since, now)
    path = path_of(root, venue, symbol)
    with file_lock(path.with_name(path.name + ".lock")):
        old = load(root, venue, symbol)
        missing = _grid(since, now).difference(old.index)
        parts = [old]
        for a, b in cd._ranges(missing):
            part = download(venue, symbol, a - pd.Timedelta(hours=1), b, **io)
            parts.append(part)
        both = pd.concat(parts).sort_index()
        _validate(both)
        added = len(both) - len(old)
        if added:
            _publish(path, both, venue, symbol)
        return added, len(both)


def coverage(root, venue, symbol, since, now):
    """Coverage is of the requested closed hours, NOT inferred listing dates, exact funding marks or liquidation."""
    since, now = cd._window(since, now)
    frame = load(root, venue, symbol)
    grid = _grid(since, now)
    observed = grid.intersection(frame.index)
    missing = grid.difference(observed)
    first = observed[0] if len(observed) else None
    return {
        **_identity(venue, symbol),
        "since": since.isoformat(),
        "through": now.floor("h").isoformat(),
        "requested_hours": len(grid),
        "observed_hours": len(observed),
        "observed_from": first.isoformat() if first is not None else None,
        "leading_unverified_hours": int((grid < first).sum()) if first is not None else len(grid),
        "missing_hours": len(missing),
        "gaps": [
            {"from": a.isoformat(), "through": b.isoformat(), "hours": int((b - a) / pd.Timedelta(hours=1)) + 1}
            for a, b in cd._ranges(missing)
        ],
        "state": "empty-window"
        if not len(grid)
        else "unavailable"
        if not len(observed)
        else "gappy"
        if len(missing)
        else "complete-requested-window",
        "complete_requested_window": bool(len(grid)) and not len(missing),
        "listing_verified": False,
        "funding_event_marks_verified": False,
        "liquidation_qualified": False,
    }
