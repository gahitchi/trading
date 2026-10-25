"""All market timestamps must fit durable signed-64-bit UTC nanoseconds."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, MarkPrice, OrderIntent, Side, Trade
from lab.markets.okx_public import MarketDataError, PublicCandle, parse_confirmed_candles


@pytest.mark.parametrize("bad", [True, 1.5, -1, 2**63])
def test_domain_source_event_timestamps_reject_boolean_fractional_or_overflow(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        Trade("venueTrade", bad, bad, Decimal("100"), Decimal("1"))
    with pytest.raises((TypeError, ValueError)):
        MarkPrice("venueMark", bad, bad, Decimal("100"))
    with pytest.raises(ValueError):
        OrderIntent("order1", "SIM:BTC", Side.BUY, Decimal("1"), bad)


def test_domain_accepts_maximal_signed_64_bit_event_timestamp() -> None:
    now = MAX_EVENT_TIMESTAMP_NS
    assert Trade("trade", now, now, Decimal("100"), Decimal("1")).received_ns == now
    assert MarkPrice("mark", now, now, Decimal("100")).exchange_ns == now
    assert OrderIntent("order1", "SIM:BTC", Side.SELL, Decimal("1"), now).created_ns == now


def test_candle_end_cannot_overflow_parquet_signed_int64_time() -> None:
    minute = 60_000_000_000
    last_start = MAX_EVENT_TIMESTAMP_NS // minute * minute
    with pytest.raises(MarketDataError, match="signed-64-bit"):
        PublicCandle(
            "BTC-USDT",
            last_start,
            last_start + minute,
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            Decimal("1"),
        )


def test_provider_future_timestamp_cannot_enter_immutable_catalog() -> None:
    far_future_ms = str((2**63 // 1_000_000 // 60_000 + 1) * 60_000)
    row = [far_future_ms, "100", "101", "99", "100", "1", "1", "100", "1"]
    with pytest.raises(MarketDataError, match="signed-64-bit"):
        parse_confirmed_candles({"code": "0", "data": [row]}, "BTC-USDT")


def test_funding_rejects_boolean_and_out_of_range_timestamp() -> None:
    from lab.markets.domain import AssetClass, ContractKind, Instrument
    from lab.markets.simulator import OfflineSimulator

    instrument = Instrument(
        "SIM",
        "BTC-USD-LINEAR",
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USD",
        Decimal("0.1"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("5"),
    )
    sim = OfflineSimulator(instrument)
    sim.on_mark(MarkPrice("m0", 0, 0, Decimal("100")))
    for bad in (True, 2**63):
        with pytest.raises(ValueError, match="timestamp"):
            sim.apply_funding("bad", Decimal("0.01"), bad)
