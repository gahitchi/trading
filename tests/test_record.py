"""The market-data recorder: everything it subscribes to lands in a readable Parquet catalog; it never trades."""

import json
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import DataActor, DataActorConfig, LoggerConfig, LogLevel
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    AggressorSide,
    BookAction,
    BookOrder,
    CryptoPerpetual,
    Currency,
    CustomData,
    DataType,
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

from lab.record import (
    MarketRecorder,
    MarketRecorderConfig,
    build_node,
    heartbeat_path,
    perpetual_ids,
    recover_stream,
    saved,
    streaming_config,
)

T0 = 1_761_000_000_000_000_000  # 2025-10-20
MS = 1_000_000
USDT = Currency.from_str("USDT")


def perpetual(symbol: str, venue: str = "binance") -> CryptoPerpetual:
    iid = perpetual_ids([symbol], venue)[0]
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


class VenueUpdate:
    """Stands in for an adapter's own data type (Binance publishes BinanceFuturesMarkPriceUpdate), which the
    Parquet writer has no schema for."""

    def __init__(self, ts):
        self.ts_event = self.ts_init = ts


class VenueExtras(DataActor):
    def on_start(self):
        self.subscribe_trades(InstrumentId.from_str("BTCUSDT-PERP.BINANCE"))

    def on_trade(self, trade):
        dt = DataType("VenueUpdate", identifier=str(trade.instrument_id))
        self.publish_data(dt, CustomData(dt, VenueUpdate(trade.ts_init)))


def record(out, instruments, events, *, venue="binance", stale_after_s=60, extra_actors=()):
    """Records like the live recorder of `venue`: its own stream directory and heartbeat, the shared catalog."""
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=LoggerConfig(stdout_level=LogLevel.ERROR), streaming=streaming_config(out, venue=venue)
        )
    )
    engine.add_venue(
        Venue(venue.upper()), OmsType.NETTING, AccountType.MARGIN, [Money(10_000, USDT)], base_currency=USDT,
        fee_model=MakerTakerFeeModel(Decimal("0.0002"), Decimal("0.0005")),
    )  # fmt: skip
    for inst in instruments:
        engine.add_instrument(inst)
    engine.add_data(events)
    recorder = MarketRecorder(
        MarketRecorderConfig(
            instrument_ids=[str(i.id) for i in instruments],
            heartbeat=str(heartbeat_path(out, venue)),
            stale_after_s=stale_after_s,
        )
    )
    engine.add_actor(recorder)
    for actor in extra_actors:
        engine.add_actor(actor)
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


def test_adapter_specific_data_is_not_streamed_and_does_not_break_saving(tmp_path):
    btc = perpetual("BTCUSDT")
    record(tmp_path, [btc], market_events(btc.id, 5), extra_actors=[VenueExtras(DataActorConfig())])

    assert saved(tmp_path) == {
        kind: {"BTCUSDT-PERP.BINANCE": n}
        for kind, n in {
            "crypto_perpetual": 1, "funding_rates": 1, "mark_prices": 1, "order_book_deltas": 2, "trades": 1
        }.items()
    }  # fmt: skip
    assert len(ParquetDataCatalog(str(tmp_path / "catalog")).query_mark_price_updates([str(btc.id)])) == 5


def test_binance_and_bybit_recordings_share_one_catalog(tmp_path):
    # One process per venue, run one after the other here: each promotes only its own files.
    binance, bybit = perpetual("BTCUSDT"), perpetual("BTCUSDT", "bybit")
    record(tmp_path, [binance], market_events(binance.id, 6))
    record(tmp_path, [bybit], market_events(bybit.id, 4), venue="bybit")

    catalog = ParquetDataCatalog(str(tmp_path / "catalog"))
    assert len(catalog.query_trade_ticks(["BTCUSDT-PERP.BINANCE"])) == 6
    assert len(catalog.query_trade_ticks(["BTCUSDT-LINEAR.BYBIT"])) == 4
    assert set(saved(tmp_path)["trades"]) == {"BTCUSDT-PERP.BINANCE", "BTCUSDT-LINEAR.BYBIT"}
    for venue, n in (("binance", 6), ("bybit", 4)):
        assert json.loads(heartbeat_path(tmp_path, venue).read_text())["instruments"].popitem()[1]["trades"] == n


def test_nothing_saved_yet(tmp_path):
    assert saved(tmp_path) == {}


def test_heartbeat_reports_counts_and_flags_a_silent_instrument(tmp_path):
    btc, eth = perpetual("BTCUSDT"), perpetual("ETHUSDT")
    late = T0 + 120 * 1_000 * MS  # ETH keeps trading two minutes after BTC went quiet
    record(tmp_path, [btc, eth], market_events(btc.id, 3) + market_events(eth.id, 3, start_ns=late))

    hb = json.loads(heartbeat_path(tmp_path, "binance").read_text())
    assert hb["running"] is False  # written at stop
    assert hb["instruments"]["BTCUSDT-PERP.BINANCE"]["trades"] == 3
    assert hb["instruments"]["ETHUSDT-PERP.BINANCE"]["last_event"].startswith("2025-10-20T")
    assert hb["stale"] == ["BTCUSDT-PERP.BINANCE"]


def test_symbols_become_perpetual_ids_of_each_venue():
    assert [str(i) for i in perpetual_ids(["btcusdt", "ETHUSDT"])] == ["BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"]
    assert [str(i) for i in perpetual_ids(["BTCUSDT"], "bybit")] == ["BTCUSDT-LINEAR.BYBIT"]
    for bad in (["BTC-USDT"], ["BTCUSDT", "btcusdt"], [], ["X"]):
        with pytest.raises(ValueError):
            perpetual_ids(bad)
    with pytest.raises(ValueError, match="unknown venue"):
        perpetual_ids(["BTCUSDT"], "kraken")


@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_the_live_recorder_node_is_data_only(tmp_path, venue):
    node, recorder = build_node(["BTCUSDT"], tmp_path, venue=venue)  # builds without connecting
    try:
        assert recorder.iids == perpetual_ids(["BTCUSDT"], venue)
        assert str(node.environment) == "LIVE"
        assert recorder.config.heartbeat == str(tmp_path / "heartbeats" / f"{venue}.json")
        assert recorder.config.book_depth == (50 if venue == "bybit" else None)  # Binance: full-depth diffs
    finally:
        node.dispose()


# Records 5 s of market data and dies by SIGKILL three seconds in, as a container killed on stop does.
KILLED_RUN = """
import os, signal, sys
from pathlib import Path
from nautilus_trader.common import DataActor
from tests.test_record import market_events, perpetual, record

class Kill(DataActor):
    trades = 0
    def on_start(self):
        self.subscribe_trades(perpetual("BTCUSDT").id)
    def on_trade(self, trade):
        self.trades += 1
        if self.trades == 3000:
            os.kill(os.getpid(), signal.SIGKILL)

btc = perpetual("BTCUSDT")
record(Path(sys.argv[1]), [btc], market_events(btc.id, 5000), extra_actors=[Kill()])
"""


def killed_run(out):
    root = Path(__file__).parent.parent
    p = subprocess.run([sys.executable, "-c", KILLED_RUN, str(out)], cwd=root, env={"PYTHONPATH": f"{root}/src:{root}"})
    assert p.returncode == -9
    return out / "stream" / "binance"


def test_a_killed_runs_unsaved_stream_is_recovered_into_the_catalog(tmp_path):
    stream = killed_run(tmp_path)
    catalog = lambda: ParquetDataCatalog(str(tmp_path / "catalog"))  # noqa: E731
    btc = ["BTCUSDT-PERP.BINANCE"]
    assert catalog().query_trade_ticks(btc) == []  # the run died before its first promotion
    (run,) = stream.glob("*/*")
    assert any(run.rglob("*.feather.partial"))

    (result,) = recover_stream(stream, tmp_path / "catalog")

    assert "error" not in result and not run.exists()
    assert len(catalog().query_trade_ticks(btc)) == 2000  # what it had flushed (each second) before dying
    assert len(catalog().query_mark_price_updates(btc)) == 2000
    assert recover_stream(stream, tmp_path / "catalog") == []
    # The next recording of the same instrument promotes beside the recovered files.
    later = T0 + 3_600_000 * MS
    reloaded = CryptoPerpetual(
        InstrumentId.from_str(btc[0]), Symbol("BTCUSDT"), Currency.from_str("BTC"), USDT, USDT, False, 1, 3,
        Price.from_str("0.1"), Quantity.from_str("0.001"), later, later,
    )  # fmt: skip
    record(tmp_path, [reloaded], market_events(reloaded.id, 10, start_ns=later))
    assert len(catalog().query_trade_ticks(btc)) == 2010


def test_a_run_that_would_overlap_the_catalog_is_left_in_place(tmp_path):
    stream = killed_run(tmp_path)
    (run,) = stream.glob("*/*")
    copy = tmp_path / "copy"
    shutil.copytree(run, copy)
    recover_stream(stream, tmp_path / "catalog")
    shutil.copytree(copy, run)
    # Recovered again (an earlier recovery that died before deleting the run): the same files, nothing to do.
    assert "error" not in recover_stream(stream, tmp_path / "catalog")[0] and not run.exists()

    shutil.copytree(copy, run)
    trades = next((tmp_path / "catalog" / "data" / "trades").rglob("*.parquet"))
    trades.rename(trades.with_name(trades.name.rsplit("_", 1)[0] + "_0000000000000000.parquet"))  # same span
    (result,) = recover_stream(stream, tmp_path / "catalog")
    assert "overlaps" in result["error"] and run.exists()
    assert not (stream / "data").exists()  # nothing half-converted left behind
