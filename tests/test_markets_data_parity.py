"""D1 data invariants: exact timestamp arithmetic, CSV headers and vectorized parity."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from lab.markets.__main__ import read_trades, timestamp_ns
from lab.markets.domain import Trade
from lab.markets.minute import MinuteBuilder
from lab.markets.minute_batch import aggregate_trades


def test_timestamp_parser_exact_utc_and_offset() -> None:
    assert timestamp_ns("1970-01-01T00:00:00Z") == 0
    assert timestamp_ns("2026-10-25T01:02:03.123456Z") % 1_000_000_000 == 123_456_000
    assert timestamp_ns("2026-10-25T03:02:03.123456+02:00") == timestamp_ns(
        "2026-10-25T01:02:03.123456Z"
    )
    with pytest.raises(ValueError, match="nonnegative"):
        timestamp_ns("1969-12-31T23:59:59Z")
    with pytest.raises(ValueError, match="timezone"):
        timestamp_ns("2026-10-25T01:02:03")


def test_csv_duplicate_headers_are_not_silently_overwritten(tmp_path) -> None:
    csv = tmp_path / "bad.csv"
    csv.write_text("timestamp,price,quantity,trade_id,mark_price,price\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CSV fields"):
        read_trades(csv)


def test_batch_research_matches_event_time_stream_for_monotonic_trades() -> None:
    times = np.asarray(
        [1, 10_000_000_000, 60_000_000_001, 60_000_000_002, 240_000_000_000],
        dtype=np.int64,
    )
    prices = np.asarray([100, 105, 110, 115, 120], dtype=np.float64)
    quantities = np.asarray([1, 3, 5, 7, 2], dtype=np.float64)
    batched = aggregate_trades(times, prices, quantities)
    streaming = MinuteBuilder()
    bars = []
    for i, (ns, price, quantity) in enumerate(zip(times, prices, quantities, strict=True)):
        bars.extend(
            streaming.push(
                Trade(
                    f"obs{i}",
                    int(ns),
                    int(ns),
                    Decimal(str(price)),
                    Decimal(str(quantity)),
                )
            )
        )
    bars.extend(streaming.flush(int(times[-1]) + 60_000_000_000))
    assert len(bars) == len(batched.start_ns)
    for i, bar in enumerate(bars):
        assert bar.start_ns == batched.start_ns[i]
        assert float(bar.open) == batched.open[i]
        assert float(bar.high) == batched.high[i]
        assert float(bar.low) == batched.low[i]
        assert float(bar.close) == batched.close[i]
        assert float(bar.volume) == batched.volume[i]
        assert bar.trades == batched.trades[i]
        assert bar.missing_minutes_before == batched.missing_minutes_before[i]
