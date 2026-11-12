"""Synthetic official-archive style bytes; never contacts Binance or sees protected data."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lab.bearish_hybrid_acquisition import acquire_binance_usdm_day

DAY = date(2024, 2, 10)
DAY_NS = 86_400_000_000_000
BOUNDARY = int(datetime(2024, 3, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
COLLECTED = datetime(2024, 3, 15, tzinfo=UTC)
SYMBOL = "ETHUSDT"
NAME = f"{SYMBOL}-1m-{DAY}.zip"


def sample(*, rows=1440, header=True, micro=False, tweak=None, member=None, extra=False):
    start_ms = int(datetime(2024, 2, 10, tzinfo=UTC).timestamp()) * 1_000
    lines = []
    if header:
        lines.append("open_time,open,high,low,close,volume,close_time,quote_asset_volume,number_of_trades,taker_buy_base_asset_volume,taker_buy_quote_asset_volume,ignore")
    for i in range(rows):
        opening_ms = start_ms + i * 60_000
        close_ms = opening_ms + 59_999
        opening = opening_ms * 1_000 if micro else opening_ms
        closing = close_ms * 1_000 + 999 if micro else close_ms
        row = [str(opening), "100", "101", "99", "100", "200", str(closing), "1000", "8", "60", "450", "0"]
        if tweak is not None:
            row = tweak(row, i)
        lines.append(",".join(row))
    raw = ("\n".join(lines) + "\n").encode()
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(NAME.removesuffix(".zip") + ".csv" if member is None else member, raw)
        if extra:
            z.writestr("other.txt", "untrusted")
    zipped = bio.getvalue()
    checksum = f"{hashlib.sha256(zipped).hexdigest()}  {NAME}\n".encode()
    return zipped, checksum


def request(root: Path, zip_raw, checksum, *, symbol=SYMBOL, day=DAY, boundary=BOUNDARY, collected=COLLECTED):
    def source(url):
        if url.endswith(".CHECKSUM"):
            return checksum
        if url.endswith(".zip"):
            return zip_raw
        raise AssertionError("unapproved URL")

    return acquire_binance_usdm_day(root, symbol, day, boundary_ns=boundary, source=source, collected_at=collected)


def test_complete_day_checksum_pin_and_raw_evidence(tmp_path):
    zipped, checksum = sample()
    report = request(tmp_path, zipped, checksum)
    assert report["rows"] == 1440
    assert report["first_end_ns"] % DAY_NS == 60_000_000_000
    assert report["last_end_ns"] % DAY_NS == 0
    assert report["zip_sha256"] == hashlib.sha256(zipped).hexdigest()
    assert report["checksum_sha256"] == hashlib.sha256(checksum).hexdigest()
    assert report["source"] == "injected-test-source"
    assert report["publisher_checksum_matched"] is True
    assert report["independent_source_authenticated"] is False
    assert report["contemporaneous_publication_verified"] is False
    assert report["oi_and_funding_available"] is False
    folder = tmp_path / "binance-usdm" / "daily-klines" / SYMBOL / str(DAY)
    assert (folder / NAME).read_bytes() == zipped
    assert (folder / (NAME + ".CHECKSUM")).read_bytes() == checksum
    assert (folder / "receipt.json").is_file()


def test_repeat_reads_immutable_first_capture_and_detects_mutation(tmp_path):
    zipped, checksum = sample()
    a = request(tmp_path, zipped, checksum)
    b = request(tmp_path, zipped, checksum, collected=datetime(2024, 4, 1, tzinfo=UTC))
    assert a == b and b["collected_at_utc"] == COLLECTED.isoformat()
    stored = tmp_path / "binance-usdm" / "daily-klines" / SYMBOL / str(DAY) / NAME
    stored.write_bytes(b"altered")
    with pytest.raises(ValueError, match="immutable"):
        request(tmp_path, zipped, checksum)


def test_wrong_checksum_blocks_all_storage(tmp_path):
    zipped, checksum = sample()
    with pytest.raises(ValueError, match="does not match"):
        request(tmp_path, zipped, b"0" * 64 + checksum[64:])
    assert not any(tmp_path.rglob("receipt.json"))


def test_wrong_checksum_filename_and_multiline_refused(tmp_path):
    zipped, checksum = sample()
    with pytest.raises(ValueError, match="filename"):
        request(tmp_path, zipped, checksum.replace(NAME.encode(), b"BTCUSDT-1m-2024-02-10.zip"))
    with pytest.raises(ValueError, match="one SHA-256 line"):
        request(tmp_path, zipped, checksum + checksum)


@pytest.mark.parametrize("rows", [1, 1439, 1441])
def test_partial_or_overlong_day_refused(tmp_path, rows):
    zipped, checksum = sample(rows=rows)
    with pytest.raises(ValueError, match="incomplete|missing, duplicate|shifted"):
        request(tmp_path, zipped, checksum)


def test_missing_or_shifted_minute_refused(tmp_path):
    def shift(row, i):
        if i == 300:
            row[0] = str(int(row[0]) + 60_000)
        return row

    zipped, checksum = sample(tweak=shift)
    with pytest.raises(ValueError, match="missing, duplicate"):
        request(tmp_path, zipped, checksum)


def test_nonfinite_and_negative_values_refused(tmp_path):
    for column, value in [(1, "NaN"), (7, "-1"), (10, "1500"), (2, "90")]:
        def damage(row, i):
            if i == 100:
                row[column] = value
            return row

        zipped, checksum = sample(tweak=damage)
        with pytest.raises(ValueError, match="invalid|nonfinite|inconsistent"):
            request(tmp_path, zipped, checksum)


def test_extra_zip_entry_and_wrong_csv_path_refused(tmp_path):
    for opts in ({"extra": True}, {"member": "../../evil.csv"}):
        zipped, checksum = sample(**opts)
        with pytest.raises(ValueError, match="expected kline CSV"):
            request(tmp_path, zipped, checksum)


def test_btc_and_post_holdout_rejected_before_network_or_storage(tmp_path):
    calls = []

    def source(url):
        calls.append(url)
        raise AssertionError("should not fetch")

    with pytest.raises(ValueError, match="non-BTC"):
        acquire_binance_usdm_day(tmp_path, "BTCUSDT", DAY, boundary_ns=BOUNDARY, source=source)
    with pytest.raises(ValueError, match="holdout"):
        acquire_binance_usdm_day(
            tmp_path, SYMBOL, DAY, boundary_ns=int(datetime(2024, 2, 11, tzinfo=UTC).timestamp()) * 1_000_000_000,
            source=source,
        )
    assert calls == []


def test_unfinished_day_rejected_before_source_call(tmp_path):
    zipped, checksum = sample()
    with pytest.raises(ValueError, match="future or unfinished"):
        request(tmp_path, zipped, checksum, collected=datetime(2024, 2, 10, 18, tzinfo=UTC))


def test_headerless_and_microsecond_timestamps_supported(tmp_path):
    zipped, checksum = sample(header=False, micro=True)
    report = request(tmp_path, zipped, checksum)
    assert report["rows"] == 1440 and report["csv_bytes"] > 0


def test_close_time_must_match_actual_minute(tmp_path):
    def shift_close(row, i):
        if i == 500:
            row[6] = str(int(row[6]) + 1_000)
        return row

    zipped, checksum = sample(tweak=shift_close)
    with pytest.raises(ValueError, match="missing, duplicate"):
        request(tmp_path, zipped, checksum)


def test_reject_invalid_symbol_and_clock_type(tmp_path):
    zipped, checksum = sample()
    with pytest.raises(ValueError, match="non-BTC"):
        request(tmp_path, zipped, checksum, symbol="../ETHUSDT")
    with pytest.raises(ValueError, match="UTC-aware"):
        request(tmp_path, zipped, checksum, collected=datetime(2024, 3, 15))


def test_wrong_source_type_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid or oversized"):
        request(tmp_path, "not bytes", b"hello")


def test_offline_load_returns_identical_closed_bars_without_fetch(tmp_path):
    from lab.bearish_hybrid_acquisition import load_captured_binance_usdm_day

    zipped, checksum = sample()
    initial = request(tmp_path, zipped, checksum)
    bars, receipt = load_captured_binance_usdm_day(tmp_path, SYMBOL, DAY, boundary_ns=BOUNDARY)
    assert len(bars) == 1440 and receipt == initial
    assert bars[0].end_ns == initial["first_end_ns"]
    assert bars[-1].end_ns == initial["last_end_ns"]


def test_offline_load_detects_receipt_manipulation(tmp_path):
    import json

    from lab.bearish_hybrid_acquisition import load_captured_binance_usdm_day

    zipped, checksum = sample()
    request(tmp_path, zipped, checksum)
    p = tmp_path / "binance-usdm" / "daily-klines" / SYMBOL / str(DAY) / "receipt.json"
    record = json.loads(p.read_text())
    record["csv_sha256"] = "f" * 64
    p.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="original acquisition receipt"):
        load_captured_binance_usdm_day(tmp_path, SYMBOL, DAY, boundary_ns=BOUNDARY)


def test_offline_load_detects_stored_checksum_manipulation(tmp_path):
    from lab.bearish_hybrid_acquisition import load_captured_binance_usdm_day

    zipped, checksum = sample()
    request(tmp_path, zipped, checksum)
    p = tmp_path / "binance-usdm" / "daily-klines" / SYMBOL / str(DAY) / (NAME + ".CHECKSUM")
    p.write_bytes(b"0" * 64 + checksum[64:])
    with pytest.raises(ValueError, match="stored publisher checksum"):
        load_captured_binance_usdm_day(tmp_path, SYMBOL, DAY, boundary_ns=BOUNDARY)


def test_offline_load_blocks_protected_boundary(tmp_path):
    from lab.bearish_hybrid_acquisition import load_captured_binance_usdm_day

    zipped, checksum = sample()
    request(tmp_path, zipped, checksum)
    with pytest.raises(ValueError, match="holdout"):
        load_captured_binance_usdm_day(
            tmp_path, SYMBOL, DAY, boundary_ns=int(datetime(2024, 2, 11, tzinfo=UTC).timestamp()) * 1_000_000_000
        )
