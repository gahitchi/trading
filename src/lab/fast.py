"""
Fast trading: one strategy, the same code in a backtest on recorded data and in paper trading on live data.

ImbalanceTaker is the first, deliberately simple, hypothesis: when the 5-level book imbalance (lab.micro) is
extreme, take liquidity in its direction and leave after a fixed holding time. Orders are limit orders at the
opposite touch, immediate-or-cancel: they take what is shown there and never sweep a thin book. It exists to exercise the
platform end to end; `lab micro-study` decides whether the imbalance is worth trading at all.

Guards: the strategy is a RiskManagedStrategy (lab.risk_manager), so every order passes the trader's risk manager
and then NautilusTrader's RiskEngine: the lab kill switch (`lab kill`), a daily loss cap that flattens and stops new
risk until the next UTC day, position and per-order notional limits, and order-rate limits. The strategy itself
holds one position at a time of a fixed size.

Paper only: `build_paper_node` pairs Binance's public market data with NautilusTrader's sandbox execution
client, which fills simulated orders against the live book. No exchange execution client, no API keys.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model import BookType, InstrumentId, OrderSide, TimeInForce
from nautilus_trader.trading import StrategyConfig

from lab.micro import IMBALANCE_LEVELS, book_imbalance
from lab.risk_manager import RiskLimits, RiskManagedStrategy, RiskManager, TradingState, add_strategy, engine_config

F_LAST = 128


class ImbalanceTakerConfig(StrategyConfig):
    def __init__(
        self,
        *,
        instrument_id: str,
        trade_size: float,
        threshold: float = 0.6,
        hold_s: float = 5.0,
        **_kwargs,
    ):
        """trade_size in base units (e.g. 0.01 BTC). Loss and position limits live in the risk manager."""
        super().__init__()
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        if not trade_size > 0 or not hold_s > 0:
            raise ValueError("trade_size and hold_s must be positive")
        self.instrument_id = instrument_id
        self.trade_size = trade_size
        self.threshold = threshold
        self.hold_s = hold_s


def fast_risk(
    venue: str,
    *,
    capital: float,
    max_loss: float,
    max_position: float | None = None,
    currency: str = "USDT",
    kill_file: str | None = None,
    state_path: str | None = None,
    audit_path: str | None = None,
) -> RiskManager:
    """The trader's risk manager for one fast strategy: max_loss is the daily loss cap; max_position (default: the
    capital, i.e. no leverage) caps the position, the gross exposure and each order's notional."""
    cap = capital if max_position is None else max_position
    limits = RiskLimits(
        currency=currency,
        max_daily_loss=max_loss,
        max_position_notional=cap,
        max_gross_notional=cap,
        max_order_notional=cap,
    )
    return RiskManager(limits, [venue], kill_file=kill_file, state_path=state_path, audit_path=audit_path)


class ImbalanceTaker(RiskManagedStrategy):
    def __init__(self, config: ImbalanceTakerConfig, risk: RiskManager):
        super().__init__(config, risk)
        self.iid = InstrumentId.from_str(config.instrument_id)
        self.entry_ns = None
        self.stopped_reason = None
        self.entries = 0  # filled entries; a rejected or unfilled IOC entry is not a trade
        self.entry_orders = set()

    def on_start(self):
        self.subscribe_book_deltas(self.iid, BookType.L2_MBP, managed=True)

    def on_stop(self):
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
        if not self.submit_order(order):  # refused by the risk manager
            self.entry_orders.discard(str(order.client_order_id))
            return None
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
        if self.risk.state is not TradingState.ACTIVE:
            if self.stopped_reason is None:
                self.stopped_reason = self.risk.reason  # the first stop of the run
            if not flat:
                self._close()  # at the touch on every book update; the risk timer retries once a second too
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


def trip_stats(trips: list[float]) -> dict:
    """Round trips, their mean P&L and its t-statistic (is the mean distinguishable from zero?)."""
    n = len(trips)
    mean = sum(trips) / n if n else 0.0
    sd = (sum((t - mean) ** 2 for t in trips) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"round_trips": n, "mean_trip": mean, "t_stat": mean / (sd / n**0.5) if sd else 0.0}


def backtest(
    catalog_path: str,
    config: ImbalanceTakerConfig,
    *,
    max_loss: float = 100.0,
    max_position: float | None = None,
    kill_file: str | None = None,
    start=None,
    end=None,
    capital: float = 10_000.0,
    maker_bps: float = 2.0,
    taker_bps: float = 5.0,
    latency_ms: float = 50.0,
) -> dict:
    """Replays recorded order-book deltas and trades through NautilusTrader's matching engine (L2 book, queue
    position, consumed liquidity) with a fixed order latency, through the same risk manager as paper trading (its
    daily loss cap is max_loss; see fast_risk). Returns the run's summary."""
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
    venue = str(inst.id.venue)
    risk = fast_risk(
        venue, capital=capital, max_loss=max_loss, max_position=max_position, currency=quote.code, kill_file=kill_file
    )
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=LoggerConfig(stdout_level=LogLevel.ERROR), risk_engine=engine_config(risk.limits, [iid])
        )
    )
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
        strategy = ImbalanceTaker(config, risk)
        add_strategy(engine, strategy, risk)
        engine.run()
        account = engine.cache.account_for_venue(Venue(str(inst.id.venue)))
        fills = [e for o in engine.cache.orders() for e in o.events() if type(e).__name__ == "OrderFilled"]
        commissions = sum(e.commission.as_double() for e in fills)
        equity = account.balance_total(quote).as_double() if account else capital
        # One closed position per round trip; NETTING keeps earlier ones as snapshots. Net of commissions.
        trips = [p.realized_pnl.as_double() for p in engine.cache.position_snapshots()]
        trips += [p.realized_pnl.as_double() for p in engine.cache.positions() if p.is_closed]
        return {
            "instrument": iid,
            "start_ns": deltas[0].ts_init,
            "end_ns": deltas[-1].ts_init,
            "entries": strategy.entries,
            "fills": len(fills),
            "commissions": commissions,
            "pnl": equity - capital,
            "stopped": strategy.stopped_reason,
            "risk_denials": dict(risk.denials),
            **trip_stats(trips),
        }
    finally:
        engine.dispose()


# ----------------------------------------------------------------------------- paper trading on live data


def build_paper_node(
    config: ImbalanceTakerConfig,
    *,
    capital: float = 10_000.0,
    taker_bps=5.0,
    maker_bps=2.0,
    max_loss: float = 100.0,
    max_position: float | None = None,
    kill_file: str | None = None,
    state_path: str | None = None,
    audit_path: str | None = None,
):
    """Binance USD-M public data + NautilusTrader's sandbox execution: simulated fills on the live book. The risk
    manager's state (the day's starting equity and any daily-loss stop) persists in state_path across restarts."""
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
    risk = fast_risk(
        venue,
        capital=capital,
        max_loss=max_loss,
        max_position=max_position,
        kill_file=kill_file,
        state_path=state_path,
        audit_path=audit_path,
    )
    node = (
        LiveNode.builder("fast-paper", TraderId("FAST-001"), Environment.LIVE)
        .add_data_client(venue, BinanceDataClientFactory(), data)
        .add_simulated_exec_client(venue, SandboxExecutionClientFactory(), sandbox)
        .with_risk_engine_config(engine_config(risk.limits, [iid], live=True))
        .build()
    )
    strategy = ImbalanceTaker(config, risk)
    add_strategy(node, strategy, risk)
    return node, strategy
