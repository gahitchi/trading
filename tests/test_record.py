"""The market-data recorder: everything it subscribes to lands in a readable Parquet catalog; it never trades."""

import json
from decimal import Decimal

import pytest
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LoggerConfig, LogLevel
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    AggressorSide,
    BookAction,
    BookOrder,
    CryptoPerpetual,
    Currency,
    FundingRateUpdate,
    InstrumentId,
    MarkPriceUpdate,
    Money,
    OmsType,
    OrderBookDelta,
    OrderSide,
    Price,
    Quantity,
    Symbol,
    TradeId,
    TradeTick,
    Venue,
)
from nautilus_trader.persistence import ParquetDataCatalog

from lab.record import MarketRecorder, MarketRecorderConfig, build_node, perpetual_ids, streaming_config

T0 = 1_761_000_000_000_000_000  # 2025-10-20
MS = 1_000_000
USDT = Currency.from_str("USDT")


def perpetual(symbol: str) -> CryptoPerpetual:
    iid = InstrumentId.from_str(f"{symbol}-PERP.BINANCE")
    return CryptoPerpetual(
        iid, Symbol(symbol), Currency.from_str(symbol[:-4]), USDT, USDT, False, 1, 3,
        Price.from_str("0.1"), Quantity.from_str("0.001"), 0, 0,
    )  # fmt: skip


def market_events(iid: InstrumentId, n: int, start_ns: int = T0) -> list:
    events = []
    for i in range(n):
        ts = start_ns + i * MS
        side = OrderSide.BUY if i % 2 == 0 else OrderSide.SELL
        price = Price.from_str(f"{100 + i}.0")
        events.append(
            OrderBookDelta(
                iid, BookAction.ADD, BookOrder(side, price, Quantity.from_str("1.000"), i + 1), 128, i + 1, ts, ts
            )
        )
        events.append(TradeTick(iid, price, Quantity.from_str("0.010"), AggressorSide.BUY, TradeId(f"t{i}"), ts, ts))
        events.append(MarkPriceUpdate(iid, price, ts, ts))
    events.append(FundingRateUpdate(iid, Decimal("0.0001"), start_ns, start_ns))
    return events


def record(out, instruments, events, *, stale_after_s=60):
    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR), streaming=streaming_config(out))
    )
    engine.add_venue(
        Venue("BINANCE"), OmsType.NETTING, AccountType.MARGIN, [Money(10_000, USDT)], base_currency=USDT,
        fee_model=MakerTakerFeeModel(Decimal("0.0002"), Decimal("0.0005")),
    )  # fmt: skip
    for inst in instruments:
        engine.add_instrument(inst)
    engine.add_data(events)
    recorder = MarketRecorder(
        MarketRecorderConfig(
            instrument_ids=[str(i.id) for i in instruments],
            heartbeat=str(out / "heartbeat.json"),
            stale_after_s=stale_after_s,
        )
    )
    engine.add_actor(recorder)
    engine.run()
    engine.dispose()  # promotes the streamed files into the catalog
    return recorder


def test_recorded_market_data_is_readable_from_the_catalog(tmp_path):
    btc, eth = perpetual("BTCUSDT"), perpetual("ETHUSDT")
    recorder = record(tmp_path, [btc, eth], market_events(btc.id, 10) + market_events(eth.id, 4))

    catalog = ParquetDataCatalog(str(tmp_path / "catalog"))
    trades = catalog.query_trade_ticks([str(btc.id)])
    assert [str(t.trade_id) for t in trades] == [f"t{i}" for i in range(10)]
    assert len(catalog.query_order_book_deltas([str(btc.id)])) == 10
    assert len(catalog.query_order_book_deltas([str(eth.id)])) == 4
    assert len(catalog.query_mark_price_updates([str(btc.id)])) == 10
    assert "FundingRateUpdate" in {str(t) for t in catalog.list_data_types()}
    assert recorder.counts[str(btc.id)] == {"book": 10, "trades": 10, "mark": 10, "funding": 1}
    assert recorder.counts[str(eth.id)] == {"book": 4, "trades": 4, "mark": 4, "funding": 1}


def test_heartbeat_reports_counts_and_flags_a_silent_instrument(tmp_path):
    btc, eth = perpetual("BTCUSDT"), perpetual("ETHUSDT")
    late = T0 + 120 * 1_000 * MS  # ETH keeps trading two minutes after BTC went quiet
    record(tmp_path, [btc, eth], market_events(btc.id, 3) + market_events(eth.id, 3, start_ns=late))

    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["running"] is False  # written at stop
    assert hb["instruments"]["BTCUSDT-PERP.BINANCE"]["trades"] == 3
    assert hb["instruments"]["ETHUSDT-PERP.BINANCE"]["last_event"].startswith("2025-10-20T")
    assert hb["stale"] == ["BTCUSDT-PERP.BINANCE"]


def test_symbols_become_binance_perpetual_ids():
    assert [str(i) for i in perpetual_ids(["btcusdt", "ETHUSDT"])] == ["BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"]
    for bad in (["BTC-USDT"], ["BTCUSDT", "btcusdt"], [], ["X"]):
        with pytest.raises(ValueError):
            perpetual_ids(bad)


def test_the_live_recorder_node_is_data_only(tmp_path):
    node, recorder = build_node(["BTCUSDT"], tmp_path)  # builds without connecting
    try:
        assert recorder.iids == perpetual_ids(["BTCUSDT"])
        assert str(node.environment) == "LIVE"
    finally:
        node.dispose()
