"""Content-addressed UTC-day Parquet catalog for read-only confirmed OKX EEA bars.

A local research artifact, NOT a trade/quote/mark/funding replay catalog. Only
confirmed provider candles are accepted; missing intervals remain observable
rather than being imputed. Exact financial decimals are preserved as strings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from lab.markets.okx_public import MarketDataError, PublicCandle

_MINUTE_NS = 60_000_000_000
_DAY_NS = 86_400_000_000_000
_SCHEMA_VERSION = 1
_SOURCE = "OKX_EEA_PUBLIC_CANDLES"
_BAR_SCHEMA = pa.schema(
    [
        pa.field("start_ns", pa.int64(), nullable=False),
        pa.field("end_ns", pa.int64(), nullable=False),
        pa.field("open", pa.string(), nullable=False),
        pa.field("high", pa.string(), nullable=False),
        pa.field("low", pa.string(), nullable=False),
        pa.field("close", pa.string(), nullable=False),
        pa.field("volume_native", pa.string(), nullable=False),
    ]
)


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _day(value: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(days=value // _DAY_NS)).date().isoformat()


def _immutable_file(path: Path, blob: bytes) -> None:
    """Exclusive create; existing different content is corruption, not an update."""
    if path.is_symlink():
        raise MarketDataError("catalog target is a symbolic link")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != blob:
            raise MarketDataError("immutable catalog content differs from existing file")
        return
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _validate(candles: tuple[PublicCandle, ...]) -> tuple[str, int]:
    if not candles:
        raise MarketDataError("cannot store an empty catalog batch")
    symbol = candles[0].instrument_id
    if not re.fullmatch(r"[A-Z0-9_-]{3,80}", symbol):
        raise MarketDataError("invalid catalog instrument ID")
    previous: int | None = None
    missing = 0
    for candle in candles:
        if candle.instrument_id != symbol or candle.source != _SOURCE:
            raise MarketDataError("mixed instrument or unqualified candle source")
        if previous is not None:
            if candle.start_ns <= previous:
                raise MarketDataError("duplicate or unsorted UTC candle")
            missing += (candle.start_ns - previous) // _MINUTE_NS - 1
        previous = candle.start_ns
    return symbol, missing


def _encode(candles: tuple[PublicCandle, ...]) -> bytes:
    data = {
        "start_ns": [c.start_ns for c in candles],
        "end_ns": [c.end_ns for c in candles],
        "open": [str(c.open) for c in candles],
        "high": [str(c.high) for c in candles],
        "low": [str(c.low) for c in candles],
        "close": [str(c.close) for c in candles],
        "volume_native": [str(c.volume_native) for c in candles],
    }
    table = pa.table(data, schema=_BAR_SCHEMA)
    output = pa.BufferOutputStream()
    pq.write_table(table, output, compression="zstd", version="2.6")
    return output.getvalue().to_pybytes()


def write_confirmed_candles(root: Path, candles: tuple[PublicCandle, ...]) -> tuple[Path, ...]:
    """Write immutable UTC-day partitions and sidecar manifests; return manifest paths.

    An identical batch is idempotent. A revised provider history produces a new
    content digest, *never* overwrites previous research evidence.
    """
    root = Path(root)
    sorted_bars = tuple(sorted(candles, key=lambda c: c.start_ns))
    symbol, _ = _validate(sorted_bars)
    by_day: dict[str, list[PublicCandle]] = defaultdict(list)
    for candle in sorted_bars:
        by_day[_day(candle.start_ns)].append(candle)
    output = []
    for day, values in sorted(by_day.items()):
        daily = tuple(values)
        _, gaps = _validate(daily)
        blob = _encode(daily)
        sha = _digest(blob)
        folder = root / "okx_eea_1m" / symbol / day
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        parquet_path = folder / f"{sha}.parquet"
        manifest_path = folder / f"{sha}.json"
        metadata = {
            "schema": _SCHEMA_VERSION,
            "source": _SOURCE,
            "instrument_id": symbol,
            "utc_date": day,
            "start_ns": daily[0].start_ns,
            "end_ns": daily[-1].end_ns,
            "rows": len(daily),
            "missing_minutes_between_observations": gaps,
            "parquet_sha256": sha,
            "price_encoding": "exact_decimal_string",
            "complete_history_claimed": False,
            "execution_feed_qualified": False,
        }
        manifest_blob = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode()
        _immutable_file(parquet_path, blob)
        _immutable_file(manifest_path, manifest_blob)
        output.append(manifest_path)
    return tuple(output)


def read_confirmed_partition(manifest_path: Path) -> tuple[PublicCandle, ...]:
    """Verify checksum, exact schema, time ordering, and manifest/partition parity."""
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise MarketDataError("catalog manifest unavailable or symbolic")
    try:
        meta = json.loads(manifest_path.read_bytes())
        sha = meta["parquet_sha256"]
        if (
            meta["schema"] != _SCHEMA_VERSION
            or meta["source"] != _SOURCE
            or not isinstance(sha, str)
            or not re.fullmatch("[a-f0-9]{64}", sha)
            or manifest_path.name != f"{sha}.json"
        ):
            raise MarketDataError("unsupported or inconsistent catalog manifest")
        if (
            set(meta)
            != {
                "schema",
                "source",
                "instrument_id",
                "utc_date",
                "start_ns",
                "end_ns",
                "rows",
                "missing_minutes_between_observations",
                "parquet_sha256",
                "price_encoding",
                "complete_history_claimed",
                "execution_feed_qualified",
            }
            or meta["complete_history_claimed"] is not False
            or meta["execution_feed_qualified"] is not False
            or meta["price_encoding"] != "exact_decimal_string"
            or manifest_path.parent.name != meta["utc_date"]
            or manifest_path.parent.parent.name != meta["instrument_id"]
        ):
            raise MarketDataError("catalog manifest qualification or location mismatch")
        parquet_path = manifest_path.with_suffix(".parquet")
        if parquet_path.is_symlink():
            raise MarketDataError("catalog payload symbolic link forbidden")
        blob = parquet_path.read_bytes()
        if _digest(blob) != sha:
            raise MarketDataError("catalog payload checksum mismatch")
        table = pq.read_table(pa.BufferReader(blob))
        if table.schema != _BAR_SCHEMA or table.num_rows != meta["rows"]:
            raise MarketDataError("catalog schema or row count mismatch")
        columns = table.to_pydict()
        candles = tuple(
            PublicCandle(
                instrument_id=meta["instrument_id"],
                start_ns=columns["start_ns"][i],
                end_ns=columns["end_ns"][i],
                open=Decimal(columns["open"][i]),
                high=Decimal(columns["high"][i]),
                low=Decimal(columns["low"][i]),
                close=Decimal(columns["close"][i]),
                volume_native=Decimal(columns["volume_native"][i]),
            )
            for i in range(table.num_rows)
        )
        symbol, gaps = _validate(candles)
        if (
            not candles
            or symbol != meta["instrument_id"]
            or _day(candles[0].start_ns) != meta["utc_date"]
            or _day(candles[-1].start_ns) != meta["utc_date"]
            or candles[0].start_ns != meta["start_ns"]
            or candles[-1].end_ns != meta["end_ns"]
            or gaps != meta["missing_minutes_between_observations"]
        ):
            raise MarketDataError("catalog payload and manifest disagree")
        return candles
    except (OSError, ValueError, TypeError, KeyError, pa.ArrowException) as exc:
        if isinstance(exc, MarketDataError):
            raise
        raise MarketDataError("invalid or unreadable immutable candle catalog") from exc
