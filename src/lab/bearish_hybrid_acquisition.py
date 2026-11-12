"""Read-only Binance USD-M daily 1m price-archive acquisition with immutable receipts.

Publisher CHECKSUM matching establishes byte integrity, not venue signature,
contemporaneous publication, original market availability or profitable signals.
Does not fetch OI, infer OI publication times, build funded signals or place orders.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from pathlib import Path
from urllib.request import Request, urlopen

from lab.bearish_hybrid import MINUTE_NS, Bar

DAY_NS = 1_440 * MINUTE_NS
BASE = "https://data.binance.vision/data/futures/um/daily/klines"
MAX_ZIP_BYTES = 8_000_000
MAX_CSV_BYTES = 12_000_000
MAX_CHECKSUM_BYTES = 1_024
ROWS_PER_DAY = 1_440


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_selection(symbol: str, day: date, boundary_ns: int) -> tuple[int, int]:
    if (
        type(symbol) is not str
        or re.fullmatch(r"[A-Z0-9]{5,20}", symbol) is None
        or not symbol.endswith("USDT")
        or symbol == "BTCUSDT"
    ):
        raise ValueError("only explicitly researched non-BTC USD-M USDT symbols are supported")
    if type(day) is not date or type(boundary_ns) is not int or boundary_ns <= 0 or boundary_ns % DAY_NS:
        raise ValueError("expected UTC calendar day and frozen UTC-midnight holdout boundary")
    start_ns = int(datetime.combine(day, time.min, UTC).timestamp()) * 1_000_000_000
    end_ns = start_ns + DAY_NS
    if start_ns <= 0 or end_ns >= boundary_ns:
        raise ValueError("archive day touches or exceeds the protected holdout")
    return start_ns, end_ns


def _https_get(url: str, limit: int) -> bytes:
    """Refuse redirects and unbounded responses; only caller-constructed URLs."""
    with urlopen(Request(url, headers={"User-Agent": "trading-lab-archive-audit/1"}), timeout=30) as response:
        if response.geturl() != url:
            raise ValueError("unverified historical archive redirect")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > limit:
            raise ValueError("historical archive response exceeds byte limit")
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("historical archive response exceeds byte limit")
    return raw


def _bounded(source: Callable[[str], bytes], url: str, limit: int) -> bytes:
    raw = source(url)
    if type(raw) is not bytes or len(raw) > limit:
        raise ValueError("invalid or oversized historical archive response")
    return raw


def _publisher_checksum(checksum: bytes, basename: str) -> str:
    try:
        raw = checksum.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("checksum is not ASCII") from exc
    parts = raw.splitlines()
    if len(parts) != 1:
        raise ValueError("checksum must contain one SHA-256 line")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([A-Za-z0-9_.-]+)", parts[0])
    if match is None or match[2] != basename:
        raise ValueError("malformed checksum or wrong historical archive filename")
    return match[1].lower()


def _millis(text: str, *, opening: bool) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Binance kline timestamp") from exc
    if value >= 10**14:
        if opening and value % 1_000:
            raise ValueError("sub-millisecond candle start is not minute aligned")
        return value // 1_000
    if value <= 0:
        raise ValueError("invalid Binance kline timestamp")
    return value


def _nonnegative(text: str, label: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not isfinite(value) or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def _checked_bars(zip_raw: bytes, basename: str, day_start_ns: int) -> tuple[tuple[Bar, ...], str, int]:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_raw)) as z:
            info = z.infolist()
            expected_csv = basename.removesuffix(".zip") + ".csv"
            if len(info) != 1 or info[0].filename != expected_csv or info[0].file_size > MAX_CSV_BYTES:
                raise ValueError("ZIP must contain exactly the bounded expected kline CSV")
            with z.open(info[0]) as stream:
                raw = stream.read(MAX_CSV_BYTES + 1)
            if len(raw) > MAX_CSV_BYTES:
                raise ValueError("decompressed kline CSV exceeds byte limit")
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise ValueError("corrupt or unsupported Binance ZIP archive") from exc
    try:
        rows = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        bars: list[Bar] = []
        for record in rows:
            if not bars and record and record[0].strip().lower().replace(" ", "_") == "open_time":
                continue
            if len(record) != 12:
                raise ValueError("Binance kline CSV row must contain 12 columns")
            opening = _millis(record[0], opening=True)
            close = _millis(record[6], opening=False)
            expected_open = day_start_ns // 1_000_000 + len(bars) * 60_000
            if opening != expected_open or close != opening + 59_999:
                raise ValueError("missing, duplicate, shifted or malformed minute candle")
            _nonnegative(record[5], "base volume")
            _nonnegative(record[9], "taker buy base volume")
            try:
                trade_count = int(record[8])
            except ValueError as exc:
                raise ValueError("invalid number of trades") from exc
            if trade_count < 0:
                raise ValueError("invalid number of trades")
            bar = Bar(
                end_ns=day_start_ns + (len(bars) + 1) * MINUTE_NS,
                open=float(record[1]),
                high=float(record[2]),
                low=float(record[3]),
                close=float(record[4]),
                quote_volume=_nonnegative(record[7], "quote volume"),
                taker_buy_quote=_nonnegative(record[10], "taker buy quote volume"),
            )
            bar.check()
            bars.append(bar)
    except (csv.Error, UnicodeError) as exc:
        raise ValueError("invalid UTF-8 or CSV quoting in historical archive") from exc
    if len(bars) != ROWS_PER_DAY:
        raise ValueError("historical archive contains an incomplete UTC day")
    return tuple(bars), _sha(raw), len(raw)


def _immutably_save(path: Path, raw: bytes) -> None:
    """Never replace a previously captured archive or audit record."""
    if path.is_symlink():
        raise ValueError("refuse historical archive symlink")
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("existing immutable archive evidence differs from fetched source")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("concurrent immutable archive capture") from exc
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def acquire_binance_usdm_day(
    root: Path,
    symbol: str,
    day: date,
    *,
    boundary_ns: int,
    source: Callable[[str], bytes] | None = None,
    collected_at: datetime | None = None,
) -> dict:
    """Collect one price-only day with official checksum and immutable raw files.

    This may contact only Binance's fixed public USD-M daily kline paths.
    An injected test source is recorded as such; no funding, OI, market
    listing or contemporaneous availability is inferred from prices.
    """
    start_ns, end_ns = _validate_selection(symbol, day, boundary_ns)
    stamp = datetime.now(UTC) if collected_at is None else collected_at
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError("collection clock must be UTC-aware")
    if stamp.timestamp() * 1_000_000_000 < end_ns:
        raise ValueError("cannot collect a future or unfinished UTC day")
    basename = f"{symbol}-1m-{day.isoformat()}.zip"
    zip_url = f"{BASE}/{symbol}/1m/{basename}"
    checksum_url = zip_url + ".CHECKSUM"
    injected = source is not None
    if source is None:
        checksum_raw = _https_get(checksum_url, MAX_CHECKSUM_BYTES)
        zip_raw = _https_get(zip_url, MAX_ZIP_BYTES)
    else:
        checksum_raw = _bounded(source, checksum_url, MAX_CHECKSUM_BYTES)
        zip_raw = _bounded(source, zip_url, MAX_ZIP_BYTES)
    expected = _publisher_checksum(checksum_raw, basename)
    zip_sha = _sha(zip_raw)
    if zip_sha != expected:
        raise ValueError("Binance archive ZIP SHA-256 does not match publisher CHECKSUM")
    bars, csv_sha, csv_bytes = _checked_bars(zip_raw, basename, start_ns)
    location = Path(root) / "binance-usdm" / "daily-klines" / symbol / day.isoformat()
    receipt_path = location / "receipt.json"
    receipt = {
        "version": 1,
        "venue": "binance-usdm",
        "symbol": symbol,
        "day": day.isoformat(),
        "first_end_ns": bars[0].end_ns,
        "last_end_ns": bars[-1].end_ns,
        "rows": len(bars),
        "zip_url": zip_url,
        "checksum_url": checksum_url,
        "zip_sha256": zip_sha,
        "checksum_sha256": _sha(checksum_raw),
        "csv_sha256": csv_sha,
        "zip_bytes": len(zip_raw),
        "csv_bytes": csv_bytes,
        "collected_at_utc": stamp.isoformat(),
        "source": "injected-test-source" if injected else "binance-public-https",
        "publisher_checksum_matched": True,
        "independent_source_authenticated": False,
        "contemporaneous_publication_verified": False,
        "oi_and_funding_available": False,
    }
    _immutably_save(location / basename, zip_raw)
    _immutably_save(location / (basename + ".CHECKSUM"), checksum_raw)
    if receipt_path.exists():
        saved = json.loads(receipt_path.read_text())
        if any(saved.get(key) != value for key, value in receipt.items() if key != "collected_at_utc"):
            raise ValueError("existing historical acquisition receipt conflicts with source")
        return saved
    _immutably_save(receipt_path, (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode())
    return receipt


def load_captured_binance_usdm_day(
    root: Path, symbol: str, day: date, *, boundary_ns: int
) -> tuple[tuple[Bar, ...], dict]:
    """Recheck saved raw ZIP, publisher checksum and receipt before offline use.

    No network I/O or feature-availability inference. Caller must separately
    preregister any performance research before accessing derived outcomes.
    """
    start_ns, _ = _validate_selection(symbol, day, boundary_ns)
    basename = f"{symbol}-1m-{day.isoformat()}.zip"
    location = Path(root) / "binance-usdm" / "daily-klines" / symbol / day.isoformat()
    raw_paths = (location / basename, location / (basename + ".CHECKSUM"), location / "receipt.json")
    if any(path.is_symlink() for path in raw_paths):
        raise ValueError("refuse historical archive evidence symlink")
    zip_raw, checksum, manifest = (path.read_bytes() for path in raw_paths)
    if len(zip_raw) > MAX_ZIP_BYTES or len(checksum) > MAX_CHECKSUM_BYTES or len(manifest) > 16_384:
        raise ValueError("stored archive evidence exceeds byte limit")
    try:
        receipt = json.loads(manifest)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid historical acquisition receipt") from exc
    expected_url = f"{BASE}/{symbol}/1m/{basename}"
    if (
        type(receipt) is not dict
        or receipt.get("version") != 1
        or receipt.get("venue") != "binance-usdm"
        or receipt.get("symbol") != symbol
        or receipt.get("day") != day.isoformat()
        or receipt.get("zip_url") != expected_url
        or receipt.get("checksum_url") != expected_url + ".CHECKSUM"
        or receipt.get("source") not in ("binance-public-https", "injected-test-source")
        or receipt.get("publisher_checksum_matched") is not True
        or receipt.get("independent_source_authenticated") is not False
        or receipt.get("contemporaneous_publication_verified") is not False
        or receipt.get("oi_and_funding_available") is not False
    ):
        raise ValueError("untrusted historical acquisition receipt")
    zip_sha = _sha(zip_raw)
    if _publisher_checksum(checksum, basename) != zip_sha:
        raise ValueError("stored publisher checksum does not match ZIP")
    bars, csv_sha, csv_bytes = _checked_bars(zip_raw, basename, start_ns)
    checks = {
        "zip_sha256": zip_sha,
        "checksum_sha256": _sha(checksum),
        "csv_sha256": csv_sha,
        "zip_bytes": len(zip_raw),
        "csv_bytes": csv_bytes,
        "rows": ROWS_PER_DAY,
        "first_end_ns": bars[0].end_ns,
        "last_end_ns": bars[-1].end_ns,
    }
    if any(receipt.get(key) != value or type(receipt.get(key)) is not type(value) for key, value in checks.items()):
        raise ValueError("stored archive does not match original acquisition receipt")
    return bars, receipt
