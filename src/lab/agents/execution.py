"""ExecutionAgent: the only component allowed to send orders."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nautilus_trader.model import OrderSide, TimeInForce
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.agents.messages import (
    T_APPROVED, T_EQUITY, T_FILL, T_RISK, T_SIGNAL, EquitySnapshot, FillReport, RiskEvent,
)
from lab.safety import is_paper_account

NY = ZoneInfo("America/New_York")


class ExecutionAgentConfig(StrategyConfig):
    def __init__(self, *, instruments: dict, venue, min_trade_value: float, capital: float,
                 rebalance_band: float = 0.02, cash_buffer: float = 0.01,
                 rebalance_time: str = "16:05", live: bool = False, paper_account: str = "",
                 state_path: str | None = None, **_kwargs):
        """instruments: ticker -> InstrumentId."""
        super().__init__()
        self.instruments = instruments
        self.venue = venue
        self.min_trade_value = min_trade_value
        self.rebalance_band = rebalance_band
        self.cash_buffer = cash_buffer
        self.capital = capital
        self.rebalance_time = rebalance_time
        self.live = live
        self.paper_account = paper_account
        self.state_path = state_path


class ExecutionAgent(Strategy):
    """Once per trading day, after the close, turns approved target weights into orders.

    - sizes positions from current equity and the day's closing prices
    - sends sells before buys; entries and exits always execute, resizes of a held
      position only when the change exceeds rebalance_band of equity
    - backtest: market orders, filled by the simulator at the next session's open print
    - paper:    market-on-open orders at Interactive Brokers, after verifying the
                broker account is a paper account
    """

    def __init__(self, config: ExecutionAgentConfig):
        super().__init__(config)
        self.weights: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.price_ts = 0
        self.new_data = False
        self.alert_pending = False
        self.halted = False
        self.rejections = 0
        self.orders_sent = 0
        self.min_cash = math.inf
        # The lab's own cash ledger: capital - purchases + sales - commissions.
        # Independent of anything else in the broker account; persisted in paper mode.
        self.cash = config.capital
        self.commissions = 0.0
        if config.state_path and Path(config.state_path).exists():
            state = json.loads(Path(config.state_path).read_text())
            self.cash = float(state["cash"])
            self.commissions = float(state.get("commissions", 0.0))

    # ------------------------------------------------------------------ lifecycle

    def on_start(self):
        self.subscribe_topic(T_SIGNAL, self._on_signal)
        self.subscribe_topic(T_APPROVED, self._on_approved)
        if self.config.live:
            self._schedule(datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY))

    def _on_signal(self, msg):
        self.prices[msg.ticker] = msg.price
        self.price_ts = max(self.price_ts, msg.ts)
        self.new_data = True
        if not self.alert_pending:
            self._schedule(datetime.fromtimestamp(msg.ts / 1e9, NY))

    def _on_approved(self, msg):
        self.weights.update(msg.weights)
        self.halted = self.halted or msg.halted

    def _schedule(self, after: datetime):
        hh, mm = map(int, self.config.rebalance_time.split(":"))
        nxt = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= after:
            nxt += timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        self.clock.set_time_alert_ns(f"rebalance-{nxt:%Y%m%d}", int(nxt.timestamp() * 1e9), callback=self._on_alert)
        self.alert_pending = True

    def _on_alert(self, event):
        self.alert_pending = False
        if self.new_data:
            self.new_data = False
            self.rebalance()
        self._schedule(datetime.fromtimestamp(event.ts_event / 1e9, NY))

    # ------------------------------------------------------------------ portfolio state

    def account_cash(self) -> float:
        """Cash according to the broker/simulator account (cross-check for the ledger)."""
        acct = self.cache.account_for_venue(self.config.venue)
        bal = acct.balance_total() if acct is not None else None
        return float(bal.as_double()) if bal is not None else float("nan")

    def _position(self, ticker: str) -> float:
        return float(self.portfolio.net_position(self.config.instruments[ticker]))

    def snapshot(self) -> EquitySnapshot:
        positions = {t: self._position(t) for t in self.config.instruments}
        invested = sum(q * self.prices.get(t, 0.0) for t, q in positions.items())
        equity = self.cash + invested
        self.min_cash = min(self.min_cash, self.cash)
        gross = invested / equity if equity > 0 else 0.0
        return EquitySnapshot(self.price_ts, equity, self.cash, positions, gross)

    # ------------------------------------------------------------------ the daily decision

    def rebalance(self):
        snap = self.snapshot()
        self.publish_message(T_EQUITY, snap)  # RiskAgent may halt here; its reply arrives synchronously
        if not math.isfinite(snap.equity) or snap.equity <= 0:
            self.log.error(f"cannot size orders: equity={snap.equity}")
            return

        orders = []
        for ticker, iid in self.config.instruments.items():
            price = self.prices.get(ticker)
            if not price:
                continue
            w = 0.0 if self.halted else self.weights.get(ticker, 0.0)
            target = math.floor(w * snap.equity * (1 - self.config.cash_buffer) / price) if w > 0 else 0
            current = snap.positions[ticker]
            diff = target - current
            if diff == 0:
                continue
            value = abs(diff) * price
            if target != 0 and value < self.config.min_trade_value:
                continue
            resize = target != 0 and current != 0
            if resize and value < self.config.rebalance_band * snap.equity:
                continue  # small drift: not worth the commission
            if self.cache.orders_open_count(instrument_id=iid) > 0:
                continue
            orders.append((diff, iid, ticker))

        orders.sort(key=lambda o: o[0] > 0)  # sells first
        for diff, iid, ticker in orders:
            self._submit(iid, ticker, diff)

    def _submit(self, iid, ticker: str, diff: float):
        if self.config.live and not self._paper_guard():
            return
        inst = self.cache.instrument(iid)
        if inst is None:
            self.log.error(f"{ticker}: instrument {iid} not loaded; order skipped")
            return
        side = OrderSide.BUY if diff > 0 else OrderSide.SELL
        tif = TimeInForce.AT_THE_OPEN if self.config.live else TimeInForce.GTC
        order = self.order_factory.market(iid, side, inst.make_qty(abs(diff)), time_in_force=tif)
        self.submit_order(order)
        self.orders_sent += 1

    def _paper_guard(self) -> bool:
        ids = [str(a.id) for a in self.cache.accounts()]
        expected = self.config.paper_account
        ok = bool(ids) and all(is_paper_account(i) and i.endswith(expected) for i in ids)
        if not ok:
            msg = f"refusing to trade: broker accounts {ids} are not the configured paper account {expected!r}"
            self.log.error(msg)
            self.publish_message(T_RISK, RiskEvent(self.clock.timestamp_ns(), "paper_guard", msg))
            self.shutdown_system(msg)
        return ok

    # ------------------------------------------------------------------ order events

    def on_order_filled(self, ev):
        qty, px = float(ev.last_qty), float(ev.last_px)
        fee = float(ev.commission.as_double())
        buy = ev.order_side == OrderSide.BUY
        self.cash += -qty * px - fee if buy else qty * px - fee
        self.commissions += fee
        if self.config.state_path:
            Path(self.config.state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.config.state_path).write_text(json.dumps({"cash": self.cash, "commissions": self.commissions}))
        self.publish_message(T_FILL, FillReport(
            ev.ts_event, str(ev.instrument_id.symbol), "BUY" if buy else "SELL", qty, px, fee,
        ))

    def on_order_rejected(self, ev):
        self.rejections += 1
        self.publish_message(T_RISK, RiskEvent(ev.ts_event, "order_rejected", f"{ev.instrument_id}: {ev.reason}"))

    def on_order_denied(self, ev):
        self.rejections += 1
        self.publish_message(T_RISK, RiskEvent(ev.ts_event, "order_denied", f"{ev.instrument_id}: {ev.reason}"))
