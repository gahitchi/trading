"""
NautilusTrader live clients backed by the replay market and the simulated broker.

They plug into the same LiveNode that `lab paper` builds for Interactive Brokers
(paper.build_node(..., clients=sim_clients(...))), so every agent, the risk and execution
engines and Nautilus's start-up reconciliation run exactly as they do against IBKR.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig, ExecutionClientConfig
from nautilus_trader.live import BarsResponse, LiveNode, RoutingConfig
from nautilus_trader.live.clients import DataClient, DataClientFactory, ExecutionClient, ExecutionClientFactory
from nautilus_trader.model import (
    AccountBalance,
    AccountId,
    AccountType,
    Bar,
    ClientOrderId,
    ExecutionMassStatus,
    FillReport,
    LiquiditySide,
    Money,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderStatusReport,
    OrderType,
    PositionSide,
    PositionStatusReport,
    Price,
    Quantity,
    TimeInForce,
    TradeId,
    TraderId,
    Venue,
    VenueOrderId,
)

from lab.sim.broker import Fill, SimBroker
from lab.universe import USD, bar_type, equity

IB = "IB"
NY = "America/New_York"


class ReplayMarket:
    """Recorded daily bars served as if the current replay day were today's completed session.

    Day `day` is dated today (New York) at `close_time`; earlier days fall on the preceding
    calendar days at the same clock time. The agents' staleness checks therefore see fresh
    data on every harness session, while prices are real history."""

    def __init__(self, frames: dict[str, pd.DataFrame], day: int):
        self.frames = frames
        self.day = day
        self.close_ns: int = 0  # wall-clock timestamp of today's (replay day's) close, set per session

    def timestamps(self, n: int) -> list[int]:
        """Timestamps for the first n replay days, the last of them at close_ns."""
        step = 86_400 * 1_000_000_000
        return [self.close_ns - (n - 1 - i) * step for i in range(n)]

    def bars(self, ticker: str, bt, start_ns: int | None) -> list[Bar]:
        df = self.frames[ticker].iloc[: self.day + 1]
        stamps = self.timestamps(len(df))
        out = []
        o, h, lo, c = (df[k].round(2).to_numpy() for k in ("open", "high", "low", "close"))
        v = df["volume"].to_numpy()
        for i, ts in enumerate(stamps):
            if start_ns is not None and ts < start_ns:
                continue
            hi, low = max(h[i], o[i], c[i]), min(lo[i], o[i], c[i])
            out.append(
                Bar(
                    bt,
                    Price(o[i], 2),
                    Price(hi, 2),
                    Price(low, 2),
                    Price(c[i], 2),
                    Quantity(max(1, int(v[i])), 0),
                    ts,
                    ts,
                )
            )
        return out

    def next_opens(self) -> dict[str, float]:
        """Opening prices of the session after the current replay day (the fills of today's orders)."""
        out = {}
        for t, df in self.frames.items():
            if self.day + 1 < len(df):
                out[t] = float(round(df["open"].iloc[self.day + 1], 2))
        return out


class ReplayDataClient(DataClient):
    def __init__(self, *, market: ReplayMarket, iids: dict, **kwargs):
        super().__init__(**kwargs)
        self.market = market
        self.tickers = {iid: t for t, iid in iids.items()}
        self.requests = 0

    async def _connect(self):
        for iid in self.tickers:
            self._handle_instrument(equity(iid))

    async def _disconnect(self):
        pass

    async def _request_bars(self, request):
        self.requests += 1
        ticker = self.tickers[request.bar_type.instrument_id]
        bars = self.market.bars(ticker, request.bar_type, request.start_ns)
        self._handle_response(
            BarsResponse(self.client_id, request.bar_type, bars, request.request_id, self.clock.timestamp_ns())
        )


class SimExecutionClient(ExecutionClient):
    """IBKR-like paper account over SimBroker. The account ID is whatever the broker reports."""

    def __init__(self, *, broker: SimBroker, iids: dict, **kwargs):
        super().__init__(**kwargs)
        self.broker = broker
        self.iids = iids
        self.tickers = {iid: t for t, iid in iids.items()}
        self.venues = {iid.venue for iid in iids.values()}

    def _handles_order_venue(self, venue) -> bool:
        # Like IBKR: one broker account routes to every listing venue (XNAS, XNYS, ...).
        return venue in self.venues or venue == self.venue

    # ------------------------------------------------------------------ connection

    async def _connect(self):
        self._send_account_state()

    async def _disconnect(self):
        pass

    def _send_account_state(self):
        cash = Money(round(self.broker.cash, 2), USD)
        self.generate_account_state([AccountBalance(cash, Money(0, USD), cash)], [], True, self.clock.timestamp_ns())

    async def _query_account(self, command):
        self._send_account_state()

    # ------------------------------------------------------------------ orders

    async def _submit_order(self, command):
        order = command.order
        ticker = self.tickers[order.instrument_id]
        order_type = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT"}.get(order.order_type, str(order.order_type))
        limit = float(order.price) if order.order_type == OrderType.LIMIT else None
        rec = self.broker.submit(
            client_order_id=str(order.client_order_id),
            ticker=ticker,
            side="BUY" if order.side == OrderSide.BUY else "SELL",
            qty=float(order.quantity),
            order_type=order_type,
            tif="AT_THE_OPEN" if order.time_in_force == TimeInForce.AT_THE_OPEN else str(order.time_in_force),
            limit=limit,
            ts=self.clock.timestamp_ns(),
        )
        self.generate_order_submitted(order)
        ts = self.clock.timestamp_ns()
        if rec["status"] == "ACCEPTED":
            self.generate_order_accepted(order, VenueOrderId(rec["venue_order_id"]), ts)
        else:
            self.generate_order_rejected(order, rec["reason"], ts, False)

    async def _cancel_order(self, command):
        self._cancel(command.client_order_id)

    async def _cancel_all_orders(self, command):
        for rec in self.broker.open_orders():
            if self.iids.get(rec["ticker"]) == command.instrument_id:
                self._cancel(ClientOrderId(rec["client_order_id"]))

    def _cancel(self, client_order_id):
        rec = self.broker.cancel(str(client_order_id), self.clock.timestamp_ns())
        order = self.cache.order(client_order_id)
        if rec is not None and order is not None:
            self.generate_order_canceled(order, VenueOrderId(rec["venue_order_id"]), self.clock.timestamp_ns())

    # ------------------------------------------------------------------ the open, while connected

    def deliver_open(self, opens: dict[str, float]) -> list[Fill]:
        """Run the opening auction now and emit the resulting events (the node is running)."""
        fills, cancelled = self.broker.open_session(opens, self.clock.timestamp_ns())
        for f in fills:
            order = self.cache.order(ClientOrderId(f.client_order_id))
            if order is None:
                continue  # not known to this node: reported through reconciliation instead
            self.generate_order_filled(
                order,
                VenueOrderId(f.venue_order_id),
                None,
                TradeId(f.trade_id),
                Quantity(f.qty, 0),
                Price(f.px, 2),
                USD,
                Money(f.commission, USD),
                LiquiditySide.TAKER,
                f.ts,
            )
        for rec in cancelled:
            order = self.cache.order(ClientOrderId(rec["client_order_id"]))
            if order is not None:
                self.generate_order_canceled(order, VenueOrderId(rec["venue_order_id"]), self.clock.timestamp_ns())
        self._send_account_state()
        return fills

    # ------------------------------------------------------------------ reconciliation reports

    def _order_report(self, rec: dict) -> OrderStatusReport:
        status = {
            "ACCEPTED": OrderStatus.PARTIALLY_FILLED if rec["filled"] else OrderStatus.ACCEPTED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
        }[rec["status"]]
        fills = self.broker.fills_for(rec["client_order_id"])
        avg = Decimal(str(round(sum(f.qty * f.px for f in fills) / sum(f.qty for f in fills), 6))) if fills else None
        return OrderStatusReport(
            self.account_id,
            self.iids[rec["ticker"]],
            VenueOrderId(rec["venue_order_id"]),
            OrderSide.BUY if rec["side"] == "BUY" else OrderSide.SELL,
            OrderType.LIMIT if rec["type"] == "LIMIT" else OrderType.MARKET,
            TimeInForce.AT_THE_OPEN if rec["tif"] == "AT_THE_OPEN" else TimeInForce.DAY,
            status,
            Quantity(rec["qty"], 0),
            Quantity(rec["filled"], 0),
            rec["ts_accepted"],
            rec["ts_last"],
            self.clock.timestamp_ns(),
            client_order_id=ClientOrderId(rec["client_order_id"]),
            price=Price(rec["limit"], 2) if rec["limit"] is not None else None,
            avg_px=avg,
        )

    def _fill_report(self, f: Fill) -> FillReport:
        return FillReport(
            self.account_id,
            self.iids[f.ticker],
            VenueOrderId(f.venue_order_id),
            TradeId(f.trade_id),
            OrderSide.BUY if f.side == "BUY" else OrderSide.SELL,
            Quantity(f.qty, 0),
            Price(f.px, 2),
            Money(f.commission, USD),
            LiquiditySide.TAKER,
            f.ts,
            self.clock.timestamp_ns(),
            client_order_id=ClientOrderId(f.client_order_id),
        )

    def _position_reports(self) -> list[PositionStatusReport]:
        out = []
        now = self.clock.timestamp_ns()
        for t, iid in self.iids.items():
            q = self.broker.position(t)
            side = PositionSide.LONG if q > 0 else PositionSide.SHORT if q < 0 else PositionSide.FLAT
            out.append(PositionStatusReport(self.account_id, iid, side, Quantity(abs(q), 0), now, now))
        return out

    def _orders(self) -> list[dict]:
        return [r for r in self.broker.state["orders"].values() if r["status"] != "REJECTED"]

    async def _generate_mass_status(self, lookback_mins):
        mass = ExecutionMassStatus(self.client_id, self.account_id, self.venue, self.clock.timestamp_ns())
        mass.add_order_reports([self._order_report(r) for r in self._orders()])
        mass.add_fill_reports([self._fill_report(f) for f in self.broker.all_fills()])
        mass.add_position_reports(self._position_reports())
        return mass

    async def _generate_order_status_report(self, command):
        rec = self.broker.state["orders"].get(str(command.client_order_id)) if command.client_order_id else None
        return self._order_report(rec) if rec else None

    async def _generate_order_status_reports(self, command):
        return [self._order_report(r) for r in self._orders()]

    async def _generate_fill_reports(self, command):
        return [self._fill_report(f) for f in self.broker.all_fills()]

    async def _generate_position_status_reports(self, command):
        return self._position_reports()


class _DataFactory(DataClientFactory):
    def __init__(self, market: ReplayMarket, iids: dict, sink: dict):
        self.market, self.iids, self.sink = market, iids, sink

    def create(self, *, name, config, cache, clock):
        client = ReplayDataClient(
            market=self.market, iids=self.iids, name=name, config=config, cache=cache, clock=clock, venue=Venue(IB)
        )
        self.sink["data"] = client
        return client


class _ExecFactory(ExecutionClientFactory):
    def __init__(self, broker: SimBroker, iids: dict, sink: dict):
        self.broker, self.iids, self.sink = broker, iids, sink

    def create(self, *, name, config, cache, clock, trader_id=None, **_kw):
        client = SimExecutionClient(
            broker=self.broker,
            iids=self.iids,
            name=name,
            config=config,
            cache=cache,
            clock=clock,
            trader_id=trader_id,
            venue=Venue(IB),
            account_id=AccountId(f"{IB}-{self.broker.account}"),
            account_type=AccountType.MARGIN,
            oms_type=OmsType.NETTING,
            base_currency=USD,
        )
        self.sink["exec"] = client
        return client


def sim_clients(market: ReplayMarket, broker: SimBroker, sink: dict):
    """A `paper.Clients` callable: the replay market and simulated broker in place of IBKR.

    `sink` receives the created clients ('data', 'exec') so the harness can drive the open."""

    def add(cfg, iids):
        routing = RoutingConfig(default=True)
        return (
            LiveNode.builder("trading-lab-harness", TraderId("LAB-001"), Environment.LIVE)
            .add_data_client(IB, _DataFactory(market, iids, sink), DataClientConfig(), routing)
            .add_exec_client(IB, _ExecFactory(broker, iids, sink), ExecutionClientConfig(), routing)
            .with_timeout_connection(10)
            .with_delay_post_stop_secs(0)
            .with_timeout_disconnection_secs(2)
        )

    return add


__all__ = ["IB", "ReplayDataClient", "ReplayMarket", "SimExecutionClient", "bar_type", "sim_clients"]
