"""
Market-data recorder: streams a crypto exchange's public order book, trades, mark prices and funding rates
into a Parquet catalog, the raw material for fast-trading research. Full order-book history is sold, not given
away; every hour recorded here is history nobody can download later.

Data only: the node has no execution client, uses no API keys and cannot place orders.

Layout (NautilusTrader's own catalog, readable with `ParquetDataCatalog(<out>/catalog)`):
  <out>/catalog/data/<type>/<instrument>/<start>_<end>_<hash>.parquet   promoted every hour and at stop
  <out>/stream/                                                       the open, not yet promoted files
  <out>/heartbeat.json                                                counts and last event per instrument

`lab monitor` serves a read-only dashboard of the heartbeat and the catalog (lab.monitor).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nautilus_trader.common import DataActor, DataActorConfig
from nautilus_trader.model import BookType, InstrumentId

from lab.persistence import atomic_json

BINANCE = "BINANCE"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
KINDS = ("book", "trades", "mark", "funding")
_SYMBOL = re.compile(r"[A-Z0-9]{2,20}")


def perpetual_ids(symbols) -> list[InstrumentId]:
    """Binance USD-M perpetual symbols ("BTCUSDT") as NautilusTrader instrument IDs ("BTCUSDT-PERP.BINANCE")."""
    out = []
    for s in symbols:
        s = str(s).upper()
        if not _SYMBOL.fullmatch(s):
            raise ValueError(f"not a Binance futures symbol: {s!r} (expected e.g. BTCUSDT)")
        out.append(InstrumentId.from_str(f"{s}-PERP.{BINANCE}"))
    if len(set(out)) != len(out):
        raise ValueError("duplicate symbols")
    if not out:
        raise ValueError("no symbols")
    return out


class MarketRecorderConfig(DataActorConfig):
    def __init__(
        self,
        *,
        instrument_ids: list[str],
        heartbeat: str | None = None,
        heartbeat_s: int = 10,
        stale_after_s: int = 60,
        **_kwargs,
    ):
        """heartbeat: JSON file rewritten every heartbeat_s seconds. An instrument whose last event is older
        than stale_after_s is listed as stale (a silent feed records nothing, which looks like a quiet market)."""
        super().__init__()
        self.instrument_ids = instrument_ids
        self.heartbeat = heartbeat
        self.heartbeat_s = heartbeat_s
        self.stale_after_s = stale_after_s


class MarketRecorder(DataActor):
    """Subscribes to everything worth keeping; the node's streaming writer stores whatever arrives."""

    def __init__(self, config: MarketRecorderConfig):
        super().__init__(config)
        self.iids = [InstrumentId.from_str(i) for i in config.instrument_ids]
        self.counts = {str(i): dict.fromkeys(KINDS, 0) for i in self.iids}
        self.last_ns = dict.fromkeys(self.counts, 0)
        self.started_ns = 0

    def on_start(self):
        self.started_ns = self.clock.timestamp_ns()
        for iid in self.iids:
            self.subscribe_book_deltas(iid, BookType.L2_MBP)
            self.subscribe_trades(iid)
            self.subscribe_mark_prices(iid)
            self.subscribe_funding_rates(iid)
        if self.config.heartbeat:
            self._write_heartbeat(running=True)
            self.clock.set_timer("heartbeat", timedelta(seconds=self.config.heartbeat_s), callback=self._on_heartbeat)

    def on_stop(self):
        if self.config.heartbeat:
            self._write_heartbeat(running=False)

    def _seen(self, kind: str, data):
        key = str(data.instrument_id)
        if key in self.counts:
            self.counts[key][kind] += 1
            self.last_ns[key] = max(self.last_ns[key], data.ts_init)

    def on_book_deltas(self, deltas):
        self._seen("book", deltas)

    def on_trade(self, trade):
        self._seen("trades", trade)

    def on_mark_price(self, mark_price):
        self._seen("mark", mark_price)

    def on_funding_rate(self, funding_rate):
        self._seen("funding", funding_rate)

    def stale(self, now_ns: int) -> list[str]:
        limit = self.config.stale_after_s * 1_000_000_000
        return sorted(k for k, last in self.last_ns.items() if now_ns - last > limit)

    def _on_heartbeat(self, _event):
        self._write_heartbeat(running=True)

    def _write_heartbeat(self, *, running: bool):
        now = self.clock.timestamp_ns()
        atomic_json(
            self.config.heartbeat,
            {
                "at": datetime.fromtimestamp(now / 1e9, UTC).isoformat(timespec="seconds"),
                "started": datetime.fromtimestamp(self.started_ns / 1e9, UTC).isoformat(timespec="seconds"),
                "running": running,
                "instruments": {
                    k: {
                        **self.counts[k],
                        "last_event": (
                            datetime.fromtimestamp(self.last_ns[k] / 1e9, UTC).isoformat(timespec="seconds")
                            if self.last_ns[k]
                            else None
                        ),
                    }
                    for k in self.counts
                },
                "stale": self.stale(now),
            },
        )


def streaming_config(out: Path, *, promote_every_s: int = 3600):
    from nautilus_trader.model import NautilusDataType as T
    from nautilus_trader.persistence import DataCatalogConfig, RotationConfig, StreamingConfig

    return StreamingConfig(
        str(out / "stream"),
        catalog=DataCatalogConfig(str(out / "catalog")),
        rotation_config=RotationConfig.interval(promote_every_s * 1_000_000_000),
        promotion_interval_ms=promote_every_s * 1000,
        promote_on_close=True,
        delete_feather_after_promotion=True,
        # Only the types the recorder is for. The Binance adapter also publishes its own data types (e.g.
        # BinanceFuturesMarkPriceUpdate) that the writer cannot serialize; unfiltered, each one is a failed
        # write and the writer fails to close, which skips the final promotion into the catalog.
        data_types=[T.Instrument, T.OrderBookDelta, T.TradeTick, T.MarkPriceUpdate, T.FundingRateUpdate],
    )


def saved(out: Path) -> dict[str, dict[str, int]]:
    """What the catalog holds: {data type: {instrument: Parquet files}}. Data reaches the catalog when it is
    promoted (every hour and at a clean stop); until then it is in <out>/stream."""
    data = Path(out) / "catalog" / "data"
    if not data.is_dir():
        return {}
    return {
        kind.name: {inst.name: len(list(inst.glob("*.parquet"))) for inst in sorted(kind.iterdir()) if inst.is_dir()}
        for kind in sorted(data.iterdir())
        if kind.is_dir()
    }


def build_node(symbols, out: Path, *, promote_every_s: int = 3600):
    """A data-only LiveNode on Binance USD-M public market data. Returns (node, recorder)."""
    from nautilus_trader.adapters.binance import (
        BinanceDataClientConfig,
        BinanceDataClientFactory,
        BinanceEnvironment,
        BinanceInstrumentProviderConfig,
        BinanceProductType,
    )
    from nautilus_trader.common import Environment
    from nautilus_trader.live import LiveNode, LiveNodeConfig
    from nautilus_trader.model import TraderId

    iids = [str(i) for i in perpetual_ids(symbols)]
    out = Path(out)
    data_client = BinanceDataClientConfig(
        product_type=BinanceProductType.USD_M,
        environment=BinanceEnvironment.LIVE,  # public market data; no keys, and no execution client below
        instrument_provider=BinanceInstrumentProviderConfig(load_all=False, load_ids=iids),
    )
    config = LiveNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId("RECORDER-001"),
        streaming=streaming_config(out, promote_every_s=promote_every_s),
        data_clients={BINANCE: data_client},
        delay_post_stop_secs=2,  # default 10s of waiting for residual events reads as a hang after Ctrl+C
    )
    if config.exec_clients:
        raise RuntimeError("the market-data recorder must not have an execution client")
    node = LiveNode.build("market-recorder", config, data_factories={BINANCE: BinanceDataClientFactory()})
    recorder = MarketRecorder(MarketRecorderConfig(instrument_ids=iids, heartbeat=str(out / "heartbeat.json")))
    node.add_actor(recorder)
    return node, recorder
