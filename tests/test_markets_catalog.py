"""Immutable, hash-addressed 24/7 1m public data are read-only research inputs."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from lab.markets.catalog import read_confirmed_partition, write_confirmed_candles
from lab.markets.okx_public import MarketDataError, PublicCandle

D = Decimal
MINUTE = 60_000_000_000
DAY = 86_400_000_000_000


def candle(start_ns: int, value: str = "100") -> PublicCandle:
    return PublicCandle("BTC-USDT", start_ns, start_ns + MINUTE, D(value), D(value), D(value), D(value), D("2.3"))


def test_immutable_catalog_preserves_exact_decimal_and_gap_metadata(tmp_path) -> None:
    bars = (candle(0, "100.123456789"), candle(MINUTE * 3, "101.123456789"))
    names = write_confirmed_candles(tmp_path, bars)
    assert len(names) == 1
    path = names[0]
    meta = json.loads(path.read_text())
    assert meta["source"] == "OKX_EEA_PUBLIC_CANDLES"
    assert meta["rows"] == 2
    assert meta["missing_minutes_between_observations"] == 2
    assert meta["complete_history_claimed"] is False
    assert meta["execution_feed_qualified"] is False
    assert read_confirmed_partition(path) == bars
    assert write_confirmed_candles(tmp_path, bars) == names


def test_provider_history_revisions_never_overwrite_existing_dataset(tmp_path) -> None:
    original = write_confirmed_candles(tmp_path, (candle(0, "100"),))
    edited = write_confirmed_candles(tmp_path, (candle(0, "101"),))
    assert original != edited
    assert read_confirmed_partition(original[0])[0].close == D("100")
    assert read_confirmed_partition(edited[0])[0].close == D("101")


def test_partitions_are_utc_day_scoped_with_complete_minute_order(tmp_path) -> None:
    bars = (candle(DAY, "102"), candle(DAY - MINUTE, "101"))
    paths = write_confirmed_candles(tmp_path, bars)
    assert len(paths) == 2
    assert [path.parent.name for path in paths] == ["1970-01-01", "1970-01-02"]
    assert sum(len(read_confirmed_partition(p)) for p in paths) == 2


def test_payload_corruption_rejected_before_replay(tmp_path) -> None:
    path = write_confirmed_candles(tmp_path, (candle(0),))[0]
    parquet = path.with_suffix(".parquet")
    parquet.write_bytes(parquet.read_bytes() + b"tampered")
    with pytest.raises(MarketDataError, match="checksum"):
        read_confirmed_partition(path)


def test_untrusted_manifest_and_symbol_rejected(tmp_path) -> None:
    with pytest.raises(MarketDataError, match="invalid catalog instrument"):
        write_confirmed_candles(tmp_path, (PublicCandle("../escape", 0, MINUTE, D("100"), D("100"), D("100"), D("100"), D("1")),))
    path = write_confirmed_candles(tmp_path, (candle(0),))[0]
    metadata = json.loads(path.read_text())
    metadata["complete_history_claimed"] = True
    # False certification is never silently accepted, even when parquet is intact.
    path.write_text(json.dumps(metadata))
    with pytest.raises(MarketDataError, match="qualification"):
        read_confirmed_partition(path)
    with pytest.raises(MarketDataError, match="unqualified"):
        write_confirmed_candles(
            tmp_path,
            (PublicCandle("BTC-USDT", MINUTE, 2 * MINUTE, D("100"), D("100"), D("100"), D("100"), D("1"), source="UNVERIFIED"),),
        )
