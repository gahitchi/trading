"""
Market-data recorder: streams a crypto exchange's public order book, trades, mark prices and funding rates
into a Parquet catalog, the raw material for fast-trading research. Full order-book history is sold, not given
away; every hour recorded here is history nobody can download later.

Venues: Binance USD-M perpetuals ("BTCUSDT-PERP.BINANCE") and Bybit linear perpetuals ("BTCUSDT-LINEAR.BYBIT").
Each venue runs in its own process, so one exchange's outage or maintenance never interrupts the other's
recording; both write into the same catalog, where every instrument has its own files.

Data only: the node has no execution client, uses no API keys and cannot place orders.

Layout (NautilusTrader's own catalog, readable with `ParquetDataCatalog(<out>/catalog)`):
  <out>/catalog/data/<type>/<instrument>/<start>_<end>_<hash>.parquet   promoted every hour and at stop
  <out>/stream/<venue>/                                               the open, not yet promoted files; what a
                                                                      killed run left is recovered at next start
  <out>/heartbeats/<venue>.json                                       counts and last event per instrument

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
BYBIT = "BYBIT"
# NautilusTrader's instrument-ID suffix for each venue's USDT-margined perpetuals.
PERPETUAL = {BINANCE: "PERP", BYBIT: "LINEAR"}
VENUES = tuple(v.lower() for v in PERPETUAL)
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
KINDS = ("book", "trades", "mark", "funding")
_SYMBOL = re.compile(r"[A-Z0-9]{2,20}")


def perpetual_ids(symbols, venue: str = "binance") -> list[InstrumentId]:
    """Perpetual symbols ("BTCUSDT") as NautilusTrader instrument IDs: "BTCUSDT-PERP.BINANCE" (Binance USD-M) or
    "BTCUSDT-LINEAR.BYBIT" (Bybit linear)."""
    v = str(venue).upper()
    if v not in PERPETUAL:
        raise ValueError(f"unknown venue: {venue!r} (expected one of {', '.join(VENUES)})")
    out = []
    for s in symbols:
        s = str(s).upper()
        if not _SYMBOL.fullmatch(s):
            raise ValueError(f"not a perpetual futures symbol: {s!r} (expected e.g. BTCUSDT)")
        out.append(InstrumentId.from_str(f"{s}-{PERPETUAL[v]}.{v}"))
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
        book_depth: int | None = None,
        **_kwargs,
    ):
        """heartbeat: JSON file rewritten every heartbeat_s seconds. An instrument whose last event is older
        than stale_after_s is listed as stale (a silent feed records nothing, which looks like a quiet market).
        book_depth: order-book levels to stream, for venues that stream a fixed depth (None: the venue's
        default; Binance streams every change to the full book)."""
        super().__init__()
        self.instrument_ids = instrument_ids
        self.heartbeat = heartbeat
        self.heartbeat_s = heartbeat_s
        self.stale_after_s = stale_after_s
        self.book_depth = book_depth


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
            self.subscribe_book_deltas(iid, BookType.L2_MBP, depth=self.config.book_depth)
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


def streaming_config(out: Path, *, promote_every_s: int = 3600, venue: str | None = None):
    """Streams into <out>/stream[/<venue>] and promotes into <out>/catalog. Each venue's process has its own
    stream directory, so neither ever promotes or cleans up the other's open files."""
    from nautilus_trader.model import NautilusDataType as T
    from nautilus_trader.persistence import DataCatalogConfig, RotationConfig, StreamingConfig

    return StreamingConfig(
        str(out / "stream" / venue if venue else out / "stream"),
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


# The streamed families worth recovering from a dead run, by stream directory. Instrument definitions are sent
# again on every start, so a dead run's copies are left out.
RECOVERED = {
    "order_book_deltas": "OrderBookDelta",
    "trades": "TradeTick",
    "mark_prices": "MarkPriceUpdate",
    "funding_rates": "FundingRateUpdate",
}
_ENVIRONMENTS = {"live": "LIVE", "sandbox": "SANDBOX", "backtest": "BACKTEST"}


def recover_stream(stream: Path, catalog: Path) -> list[dict]:
    """Saves into `catalog` what earlier runs left unpromoted in `stream` (<stream>/<environment>/<run>/), then
    deletes those runs. A run killed before it could save (SIGKILL, a stop that timed out) leaves its last hour
    there, and no later run ever promotes it. Call only before this stream's recorder starts: every run found is
    taken for dead. A run whose data would overlap the catalog, or that fails to convert, is left in place.
    Returns {run, files, bytes, error} per run found with data."""
    import shutil

    from nautilus_trader.common import Environment
    from nautilus_trader.model import NautilusDataType as T
    from nautilus_trader.persistence import ParquetDataCatalog

    from lab.monitor import file_span

    staging = stream / "data"  # where a catalog rooted at the stream writes what it converts
    results = []
    for env, env_name in _ENVIRONMENTS.items():
        runs = sorted(p for p in (stream / env).glob("*") if p.is_dir())
        for run in runs:
            files = [f for f in run.rglob("*") if f.is_file() and not f.name.startswith("_nautilus")]
            if not files:
                continue
            result = {"run": str(run), "files": len(files), "bytes": sum(f.stat().st_size for f in files)}
            results.append(result)
            shutil.rmtree(staging, ignore_errors=True)
            try:
                source = ParquetDataCatalog(str(stream))
                for name, data_type in RECOVERED.items():
                    if (run / name).is_dir():
                        source.convert_stream_to_data(
                            run.name, getattr(T, data_type), environment=getattr(Environment, env_name)
                        )
                moves = []
                for f in sorted(staging.rglob("*.parquet")):
                    dest = catalog / "data" / f.relative_to(staging)
                    if dest.exists():
                        continue  # moved by an earlier recovery that died before deleting the run
                    a, b = file_span(f.name)
                    for other in dest.parent.glob("*.parquet"):
                        span = file_span(other.name)
                        if span and span[0] <= b and a <= span[1]:
                            raise ValueError(f"{f.relative_to(staging)} overlaps {other.name} in the catalog")
                    moves.append((f, dest))
                for f, dest in moves:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(f, dest)
                shutil.rmtree(run)
            except Exception as e:  # a run that can't be recovered must not stop the recording
                result["error"] = f"{type(e).__name__}: {e}"
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    return results


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


def heartbeat_path(out: Path, venue: str) -> Path:
    return Path(out) / "heartbeats" / f"{venue.lower()}.json"


# Bybit streams the book at a fixed depth: 1, 50, 200 or 1000 levels, pushed every 10, 20, 100 or 200 ms on linear
# contracts. 50 levels at 20 ms keeps the fastest updates with depth well past what the features read.
BYBIT_BOOK_DEPTH = 50


def _data_client(venue: str, iids: list[str]):
    """(config, factory) for a venue's public market data. No keys: nothing here can trade."""
    if venue == BINANCE:
        from nautilus_trader.adapters.binance import (
            BinanceDataClientConfig,
            BinanceDataClientFactory,
            BinanceEnvironment,
            BinanceInstrumentProviderConfig,
            BinanceProductType,
        )

        return BinanceDataClientConfig(
            product_type=BinanceProductType.USD_M,
            environment=BinanceEnvironment.LIVE,
            instrument_provider=BinanceInstrumentProviderConfig(load_all=False, load_ids=iids),
        ), BinanceDataClientFactory()
    from nautilus_trader.adapters.bybit import (
        BybitDataClientConfig,
        BybitDataClientFactory,
        BybitEnvironment,
        BybitProductType,
    )

    return BybitDataClientConfig(
        product_types=[BybitProductType.LINEAR], environment=BybitEnvironment.MAINNET
    ), BybitDataClientFactory()


def build_node(symbols, out: Path, *, venue: str = "binance", promote_every_s: int = 3600):
    """A data-only LiveNode on one venue's public perpetual-futures market data. Returns (node, recorder)."""
    from nautilus_trader.common import Environment
    from nautilus_trader.live import LiveNode, LiveNodeConfig
    from nautilus_trader.model import TraderId

    iids = [str(i) for i in perpetual_ids(symbols, venue)]
    v = venue.upper()
    out = Path(out)
    data_client, factory = _data_client(v, iids)
    config = LiveNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId(f"RECORDER-{v}"),
        streaming=streaming_config(out, promote_every_s=promote_every_s, venue=venue.lower()),
        data_clients={v: data_client},
        delay_post_stop_secs=2,  # default 10s of waiting for residual events reads as a hang after Ctrl+C
    )
    if config.exec_clients:
        raise RuntimeError("the market-data recorder must not have an execution client")
    node = LiveNode.build(f"market-recorder-{venue.lower()}", config, data_factories={v: factory})
    recorder = MarketRecorder(
        MarketRecorderConfig(
            instrument_ids=iids,
            heartbeat=str(heartbeat_path(out, venue)),
            book_depth=BYBIT_BOOK_DEPTH if v == BYBIT else None,
        )
    )
    node.add_actor(recorder)
    return node, recorder
