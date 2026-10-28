"""
Fast trading: one strategy, the same code in a backtest on recorded data and in paper trading on live data.

ImbalanceTaker is the first, deliberately simple, hypothesis: when the 5-level book imbalance (lab.micro) is
extreme, take liquidity in its direction and leave after a fixed holding time. Orders are limit orders at the
opposite touch, immediate-or-cancel: they take what is shown there and never sweep a thin book. It exists to exercise the
platform end to end; `lab micro-study` decides whether the imbalance is worth trading at all.

Guards: the lab kill switch (`lab kill` writes it; checked every second, any kill file stops new orders and
flattens), a loss limit for the run that flattens and stops trading, one position at a time of a fixed size,
and NautilusTrader's order-rate limit.

Paper only: `build_paper_node` pairs Binance's public market data with NautilusTrader's sandbox execution
client, which fills simulated orders against the live book. No exchange execution client, no API keys.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from nautilus_trader.model import BookType, InstrumentId, OrderSide, TimeInForce
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.micro import IMBALANCE_LEVELS, book_imbalance
from lab.safety import read_kill_switch

F_LAST = 128


class ImbalanceTakerConfig(StrategyConfig):
    def __init__(
        self,
        *,
        instrument_id: str,
        trade_size: float,
        threshold: float = 0.6,
        hold_s: float = 5.0,
        max_loss: float = 100.0,
        kill_file: str | None = None,
        **_kwargs,
    ):
        """trade_size in base units (e.g. 0.01 BTC); max_loss in the quote currency for the whole run."""
        super().__init__()
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        if not trade_size > 0 or not hold_s > 0 or not max_loss > 0:
            raise ValueError("trade_size, hold_s and max_loss must be positive")
        self.instrument_id = instrument_id
        self.trade_size = trade_size
        self.threshold = threshold
        self.hold_s = hold_s
        self.max_loss = max_loss
        self.kill_file = kill_file


class ImbalanceTaker(Strategy):
    def __init__(self, config: ImbalanceTakerConfig):
        super().__init__(config)
        self.iid = InstrumentId.from_str(config.instrument_id)
        self.entry_ns = None
        self.stopped_reason = None
        self.entries = 0  # filled entries; a rejected or unfilled IOC entry is not a trade
        self.entry_orders = set()

    def on_start(self):
        self.subscribe_book_deltas(self.iid, BookType.L2_MBP, managed=True)
        if self.config.kill_file:
            self.clock.set_timer("kill-check", timedelta(seconds=1), callback=self._on_kill_check)

    def on_stop(self):
        self._flatten()

    def _on_kill_check(self, _event):
        kill = read_kill_switch(self.config.kill_file)
        if kill:
            self._halt(f"kill switch: {kill.get('reason', '')}")

    def _halt(self, reason: str):
        if self.stopped_reason is None:
            self.stopped_reason = reason
            self.log.warning(f"trading stopped: {reason}")
        self._flatten()

    def _flatten(self):
        self.cancel_all_orders(self.iid)
        self._close()

    def _close(self):
        """Exit the whole position at the touch; an IOC that misses is retried on the next book update."""
        net = float(self.portfolio.net_position(self.iid))
        if net:
            self._take(OrderSide.SELL if net > 0 else OrderSide.BUY, abs(net), reduce_only=True)

    def _take(self, side, qty: float, *, reduce_only: bool = False, entry: bool = False):
        """A limit order at the opposite touch, immediate-or-cancel: takes what is shown there, never pays
        through it (a market order would sweep the book on a thin moment)."""
        book = self.cache.order_book(self.iid)
        price = book.best_ask_price() if side == OrderSide.BUY else book.best_bid_price() if book else None
        if price is None:
            return None
        inst = self.cache.instrument(self.iid)
        order = self.order_factory.limit(
            self.iid, side, inst.make_qty(qty), price, time_in_force=TimeInForce.IOC, reduce_only=reduce_only
        )
        if entry:
            self.entry_orders.add(str(order.client_order_id))  # before submitting: a fill can arrive at once
        self.submit_order(order)
        return order

    def on_order_filled(self, event):
        coid = str(event.client_order_id)
        if coid in self.entry_orders:
            self.entry_orders.discard(coid)  # count an entry once, on its first fill
            self.entries += 1

    def _busy(self) -> bool:
        return bool(self.cache.orders_open(instrument_id=self.iid, strategy_id=self.strategy_id))

    def on_book_deltas(self, deltas):
        if not deltas.flags & F_LAST or self._busy():
            return
        book = self.cache.order_book(self.iid)
        if book is None:
            return
        flat = self.portfolio.is_net_flat(self.iid)
        if self.stopped_reason is not None:
            if not flat:
                self._close()
            return
        pnl = self.portfolio.total_pnl(self.iid)
        if pnl is not None and pnl.as_double() <= -self.config.max_loss:
            self._halt(f"loss limit: {pnl}")
            return
        now = self.clock.timestamp_ns()
        if not flat:
            if self.entry_ns is not None and now - self.entry_ns >= self.config.hold_s * 1e9:
                self._close()
            return
        bids = [(float(lv.price), lv.size()) for lv in book.bids(IMBALANCE_LEVELS)]
        asks = [(float(lv.price), lv.size()) for lv in book.asks(IMBALANCE_LEVELS)]
        imb = book_imbalance(bids, asks)
        if imb is None or abs(imb.imbalance) < self.config.threshold:
            return
        side = OrderSide.BUY if imb.imbalance > 0 else OrderSide.SELL
        if self._take(side, self.config.trade_size, entry=True) is not None:
            self.entry_ns = now


# ----------------------------------------------------------------------------- backtest on recorded data


def fee_model(maker_bps: float, taker_bps: float):
    from nautilus_trader.execution import MakerTakerFeeModel

    return MakerTakerFeeModel(Decimal(str(maker_bps)) / 10_000, Decimal(str(taker_bps)) / 10_000)


def backtest(
    catalog_path: str,
    config: ImbalanceTakerConfig,
    *,
    start=None,
    end=None,
    capital: float = 10_000.0,
    maker_bps: float = 2.0,
    taker_bps: float = 5.0,
    latency_ms: float = 50.0,
) -> dict:
    """Replays recorded order-book deltas and trades through NautilusTrader's matching engine (L2 book, queue
    position, consumed liquidity) with a fixed order latency. Returns the run's summary."""
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.common import LoggerConfig, LogLevel
    from nautilus_trader.execution import StaticLatencyModel
    from nautilus_trader.model import AccountType, Money, OmsType, Venue
    from nautilus_trader.persistence import ParquetDataCatalog

    catalog = ParquetDataCatalog(catalog_path)
    iid = config.instrument_id
    instruments = catalog.instruments(instrument_ids=[iid])
    if not instruments:
        raise ValueError(f"{iid} is not in {catalog_path}; record it first")
    inst = instruments[-1]
    deltas = catalog.query_order_book_deltas([iid], start, end)
    trades = catalog.query_trade_ticks([iid], start, end)
    if not deltas:
        raise ValueError(f"no order-book data for {iid} in that period")
    quote = inst.quote_currency
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
    try:
        engine.add_venue(
            Venue(str(inst.id.venue)),
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money(capital, quote)],
            base_currency=quote,
            default_leverage=Decimal(1),
            book_type=BookType.L2_MBP,
            fee_model=fee_model(maker_bps, taker_bps),
            latency_model=StaticLatencyModel(base_latency_nanos=int(latency_ms * 1e6)),
            queue_position=True,
            liquidity_consumption=True,
        )
        engine.add_instrument(inst)
        engine.add_data(deltas)
        if trades:
            engine.add_data(trades)
        strategy = ImbalanceTaker(config)
        engine.add_strategy(strategy)
        engine.run()
        account = engine.cache.account_for_venue(Venue(str(inst.id.venue)))
        fills = [e for o in engine.cache.orders() for e in o.events() if type(e).__name__ == "OrderFilled"]
        commissions = sum(e.commission.as_double() for e in fills)
        equity = account.balance_total(quote).as_double() if account else capital
        return {
            "instrument": iid,
            "start_ns": deltas[0].ts_init,
            "end_ns": deltas[-1].ts_init,
            "entries": strategy.entries,
            "fills": len(fills),
            "commissions": commissions,
            "pnl": equity - capital,
            "stopped": strategy.stopped_reason,
        }
    finally:
        engine.dispose()


# ----------------------------------------------------------------------------- paper trading on live data


def build_paper_node(config: ImbalanceTakerConfig, *, capital: float = 10_000.0, taker_bps=5.0, maker_bps=2.0):
    """Binance USD-M public data + NautilusTrader's sandbox execution: simulated fills on the live book."""
    from nautilus_trader.adapters.binance import (
        BinanceDataClientConfig,
        BinanceDataClientFactory,
        BinanceEnvironment,
        BinanceInstrumentProviderConfig,
        BinanceProductType,
    )
    from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig, SandboxExecutionClientFactory
    from nautilus_trader.common import Environment
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import AccountType, Currency, Money, OmsType, TraderId, Venue

    iid = InstrumentId.from_str(config.instrument_id)
    venue = str(iid.venue)
    usdt = Currency.from_str("USDT")
    data = BinanceDataClientConfig(
        product_type=BinanceProductType.USD_M,
        environment=BinanceEnvironment.LIVE,  # public market data only
        instrument_provider=BinanceInstrumentProviderConfig(load_all=False, load_ids=[str(iid)]),
    )
    sandbox = SandboxExecutionClientConfig(
        Venue(venue),
        [Money(capital, usdt)],
        base_currency=usdt,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        default_leverage=Decimal(1),
        book_type=BookType.L2_MBP,
        fee_model=fee_model(maker_bps, taker_bps),
        queue_position=True,
        liquidity_consumption=True,
    )
    node = (
        LiveNode.builder("fast-paper", TraderId("FAST-001"), Environment.LIVE)
        .add_data_client(venue, BinanceDataClientFactory(), data)
        .add_simulated_exec_client(venue, SandboxExecutionClientFactory(), sandbox)
        .build()
    )
    strategy = ImbalanceTaker(config)
    node.add_strategy(strategy)
    return node, strategy
