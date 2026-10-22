"""ExecutionAgent: the only component allowed to send orders."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nautilus_trader.model import ClientOrderId, OrderSide, TimeInForce
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.agents.messages import (
    T_APPROVED,
    T_EQUITY,
    T_FILL,
    T_RISK,
    T_SIGNAL,
    EquitySnapshot,
    FillReport,
    RiskEvent,
)
from lab.agents.schedule import next_at
from lab.safety import is_paper_account, read_kill_switch

NY = ZoneInfo("America/New_York")
_KEEP_TRADE_IDS = 2000  # broker trade IDs remembered for de-duplication (far more than a few days of fills)


class ExecutionAgentConfig(StrategyConfig):
    def __init__(
        self,
        *,
        instruments: dict,
        venue,
        min_trade_value: float,
        capital: float,
        rebalance_band: float = 0.02,
        cash_buffer: float = 0.01,
        rebalance_time: str = "16:05",
        max_order_value: float = math.inf,
        max_orders_per_day: int = 1_000,
        max_daily_turnover: float = math.inf,
        live: bool = False,
        paper_account: str = "",
        state_path: str | None = None,
        kill_path: str | None = None,
        heartbeat_path: str | None = None,
        adopt_broker_positions: bool = False,
        weekdays_only: bool = True,
        **_kwargs,
    ):
        """instruments: ticker -> InstrumentId.

        max_order_value:    no single buy larger than this (USD)
        max_orders_per_day: hard cap on orders per rebalance day
        max_daily_turnover: value bought per day / equity (1.0 = the whole portfolio)
        """
        super().__init__()
        self.instruments = instruments
        self.venue = venue
        self.min_trade_value = min_trade_value
        self.capital = capital
        self.rebalance_band = rebalance_band
        self.cash_buffer = cash_buffer
        self.rebalance_time = rebalance_time
        self.max_order_value = max_order_value
        self.max_orders_per_day = max_orders_per_day
        self.max_daily_turnover = max_daily_turnover
        self.live = live
        self.paper_account = paper_account
        self.state_path = state_path
        self.kill_path = kill_path
        self.heartbeat_path = heartbeat_path
        self.adopt_broker_positions = adopt_broker_positions
        self.weekdays_only = weekdays_only


class ExecutionAgent(Strategy):
    """Once per trading day, after the close, turns approved target weights into orders.

    - sizes positions from the lab's own equity ledger and the day's closing prices
    - sends sells before buys; entries and exits always execute, resizes of a held
      position only when the change exceeds rebalance_band of equity
    - refuses to trade when its ledger disagrees with the broker's positions
      (reconciled before every rebalance), when the kill switch is set, or past the
      daily order/turnover caps
    - backtest: market orders, filled by the simulator at the next session's open print
    - paper:    market-on-open orders at Interactive Brokers, after verifying the
                broker account is the configured paper account
    """

    def __init__(self, config: ExecutionAgentConfig):
        super().__init__(config)
        self.weights: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.price_ts = 0
        self.new_data = False
        self.alert_pending = False
        self.halted = False
        self.blocked_reason = ""
        self.rejections = 0
        self.orders_sent = 0
        self.min_cash = math.inf
        # The lab's own ledger: capital - purchases + sales - commissions, and the shares
        # it believes it holds. Independent of anything else in the broker account.
        self.cash = config.capital
        self.commissions = 0.0
        self.held: dict[str, float] = dict.fromkeys(config.instruments, 0.0)
        # Write-ahead order journal: every order is recorded here BEFORE it is sent, and stays
        # until the broker reports it closed. After a crash between order and fill, the fills are
        # recovered from the broker's reports at start-up (_recover_pending) instead of leaving the
        # ledger behind the broker. Fills are applied once per broker trade ID.
        self.pending: dict[str, dict] = {}
        self.applied: list[str] = []
        self._applied_set: set[str] = set()
        if config.state_path and Path(config.state_path).exists():
            state = json.loads(Path(config.state_path).read_text())
            self.cash = float(state["cash"])
            self.commissions = float(state.get("commissions", 0.0))
            self.held.update({t: float(q) for t, q in state.get("positions", {}).items() if t in self.held})
            self.pending = dict(state.get("pending", {}))
            self.applied = list(state.get("applied_trades", []))
            self._applied_set = set(self.applied)

    # ------------------------------------------------------------------ lifecycle

    def on_start(self):
        self.subscribe_topic(T_SIGNAL, self._on_signal)
        self.subscribe_topic(T_APPROVED, self._on_approved)
        if self.config.live:
            now = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY)
            self._recover_pending()
            if self.config.adopt_broker_positions:
                self._adopt_broker_positions()
            self._schedule(now)
            self.clock.set_timer("heartbeat", timedelta(seconds=60), callback=self._on_heartbeat)
            self._on_heartbeat(None)
            self._risk_event(
                "startup",
                f"execution agent started; ledger cash {self.cash:,.2f}, positions {self._nonzero(self.held)}",
            )

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
        nxt = next_at(after, self.config.rebalance_time, weekdays_only=self.config.weekdays_only)
        self.clock.set_time_alert_ns(
            f"rebalance-{nxt:%Y%m%d-%H%M%S}", int(nxt.timestamp() * 1e9), callback=self._on_alert
        )
        self.alert_pending = True

    def _on_alert(self, event):
        self.alert_pending = False
        if self.new_data:
            self.new_data = False
            self.rebalance()
        self._schedule(datetime.fromtimestamp(event.ts_event / 1e9, NY))

    def _on_heartbeat(self, _event):
        if self.pending:
            self._recover_pending()  # safety net: settle journaled orders from the broker's latest state
        kill = self._kill()
        if kill:
            self._cancel_open_orders()
        if self.config.heartbeat_path:
            p = Path(self.config.heartbeat_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {
                        "at": datetime.now(UTC).isoformat(timespec="seconds"),
                        "cash": round(self.cash, 2),
                        "positions": self._nonzero(self.held),
                        "kill_switch": kill["mode"] if kill else None,
                        "halted": self.halted,
                        "blocked": self.blocked_reason or None,
                        "orders_sent": self.orders_sent,
                        "account": self._account_id(),
                    }
                )
            )

    # ------------------------------------------------------------------ state

    @staticmethod
    def _nonzero(d: dict) -> dict:
        return {k: v for k, v in d.items() if v}

    def account_cash(self) -> float:
        """Cash according to the broker/simulator account (cross-check for the ledger)."""
        acct = self.cache.account_for_venue(self.config.venue)
        bal = acct.balance_total() if acct is not None else None
        return float(bal.as_double()) if bal is not None else float("nan")

    def _broker_position(self, ticker: str) -> float:
        return float(self.portfolio.net_position(self.config.instruments[ticker]))

    def reconcile(self) -> list[str]:
        """Differences between the ledger and the broker's positions, as messages."""
        diffs = []
        for t in self.config.instruments:
            b, mine = self._broker_position(t), self.held.get(t, 0.0)
            if abs(b - mine) > 1e-9:
                diffs.append(f"{t}: ledger {mine:g} vs broker {b:g}")
        return diffs

    def _adopt_broker_positions(self):
        """Operator-requested reset: take the broker's positions as truth, at their cost basis."""
        adopted = {}
        for t, iid in self.config.instruments.items():
            q = self._broker_position(t)
            if q:
                positions = self.cache.positions_open(instrument_id=iid)
                v = positions[0].avg_px_open if positions else 0.0
                avg = float(v() if callable(v) else v)
                adopted[t] = (q, avg)
        self.held = dict.fromkeys(self.config.instruments, 0.0)
        self.cash = self.config.capital
        for t, (q, avg) in adopted.items():
            self.held[t] = q
            self.cash -= q * avg
        self._save()
        self._risk_event("ledger_reset", f"adopted broker positions {adopted}; ledger cash now {self.cash:,.2f}")

    def snapshot(self) -> EquitySnapshot:
        invested = sum(q * self.prices.get(t, 0.0) for t, q in self.held.items())
        equity = self.cash + invested
        self.min_cash = min(self.min_cash, self.cash)
        gross = invested / equity if equity > 0 else 0.0
        return EquitySnapshot(self.price_ts, equity, self.cash, dict(self.held), gross)

    def _save(self):
        if self.config.state_path:
            p = Path(self.config.state_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "cash": self.cash,
                        "commissions": self.commissions,
                        "positions": self._nonzero(self.held),
                        "pending": self.pending,
                        "applied_trades": self.applied[-_KEEP_TRADE_IDS:],
                        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                )
            )
            tmp.replace(p)  # atomic: a crash never leaves a half-written ledger

    def _kill(self) -> dict | None:
        return read_kill_switch(self.config.kill_path) if self.config.kill_path else None

    def _risk_event(self, kind: str, detail: str):
        self.publish_message(T_RISK, RiskEvent(self.clock.timestamp_ns(), kind, detail))

    def _cancel_open_orders(self):
        for iid in self.config.instruments.values():
            if self.cache.orders_open_count(instrument_id=iid) > 0:
                self.cancel_all_orders(iid)

    # ------------------------------------------------------------------ the daily decision

    def rebalance(self):
        snap = self.snapshot()
        self.publish_message(T_EQUITY, snap)  # RiskAgent may halt here; its reply arrives synchronously
        if not math.isfinite(snap.equity) or snap.equity <= 0:
            self._risk_event("blocked", f"cannot size orders: equity={snap.equity}")
            return

        diffs = self.reconcile()
        if diffs:
            self.blocked_reason = "ledger and broker disagree: " + "; ".join(diffs)
            self._risk_event(
                "reconciliation_mismatch",
                self.blocked_reason + ". No orders until resolved (lab paper --adopt-broker resets the ledger).",
            )
            return
        self.blocked_reason = ""

        kill = self._kill()
        flatten = bool(kill and kill.get("mode") == "flatten")
        if kill and not flatten:
            self._cancel_open_orders()
            self._risk_event("kill_switch", f"halted by kill switch ({kill.get('reason', '')}); no orders sent")
            return

        orders = []
        for ticker, iid in self.config.instruments.items():
            price = self.prices.get(ticker)
            if not price:
                continue
            w = 0.0 if (self.halted or flatten) else self.weights.get(ticker, 0.0)
            target = math.floor(w * snap.equity * (1 - self.config.cash_buffer) / price) if w > 0 else 0
            current = self.held.get(ticker, 0.0)
            diff = target - current
            if diff == 0:
                continue
            value = abs(diff) * price
            if target != 0 and value < self.config.min_trade_value:
                continue
            if target != 0 and current != 0 and value < self.config.rebalance_band * snap.equity:
                continue  # small drift: not worth the commission
            if self.cache.orders_open_count(instrument_id=iid) > 0:
                continue
            if any(p["ticker"] == ticker for p in self.pending.values()):
                # An earlier order for this stock is not settled yet: never risk a duplicate.
                self._risk_event("order_pending", f"{ticker}: previous order still unsettled; no new order today")
                continue
            orders.append((diff, iid, ticker, price))

        orders.sort(key=lambda o: o[0] > 0)  # sells first
        sent = 0
        bought = 0.0
        for diff, iid, ticker, price in orders:
            if diff > 0 and diff * price > self.config.max_order_value:
                diff = math.floor(self.config.max_order_value / price)
                self._risk_event("order_capped", f"{ticker} buy capped at {self.config.max_order_value:,.0f} USD")
                if diff <= 0:
                    continue
            if sent >= self.config.max_orders_per_day:
                self._risk_event("daily_cap", f"max {self.config.max_orders_per_day} orders/day reached; rest deferred")
                break
            if diff > 0 and (bought + diff * price) / snap.equity > self.config.max_daily_turnover:
                self._risk_event(
                    "daily_cap", f"daily buy cap {self.config.max_daily_turnover:.0%} of equity reached; rest deferred"
                )
                break
            if self._submit(iid, ticker, diff):
                sent += 1
                if diff > 0:
                    bought += diff * price

    def _submit(self, iid, ticker: str, diff: float) -> bool:
        if self.config.live and not self._paper_guard():
            return False
        inst = self.cache.instrument(iid)
        if inst is None:
            self._risk_event("blocked", f"{ticker}: instrument {iid} not loaded; order skipped")
            return False
        side = OrderSide.BUY if diff > 0 else OrderSide.SELL
        tif = TimeInForce.AT_THE_OPEN if self.config.live else TimeInForce.GTC
        order = self.order_factory.market(iid, side, inst.make_qty(abs(diff)), time_in_force=tif)
        self.pending[str(order.client_order_id)] = {
            "ticker": ticker,
            "side": "BUY" if diff > 0 else "SELL",
            "qty": abs(diff),
            "ts": self.clock.timestamp_ns(),
        }
        self._save()  # journaled before it can reach the broker
        self.submit_order(order)
        self.orders_sent += 1
        return True

    def _account_id(self) -> str | None:
        """The account the broker reports for our venue (e.g. 'IB-DU1234567'), or None before it reports."""
        aid = self.cache.account_id(self.config.venue)
        return str(aid) if aid is not None else None

    def _paper_guard(self) -> bool:
        account = self._account_id()
        expected = self.config.paper_account
        ok = account is not None and is_paper_account(account) and account.split("-", 1)[-1] == expected
        if not ok:
            msg = f"refusing to trade: broker account {account!r} is not the configured paper account {expected!r}"
            self.log.error(msg)
            self._risk_event("paper_guard", msg)
            self.shutdown_system(msg)
        return ok

    # ------------------------------------------------------------------ order events

    def _apply_fill(self, ev, *, recovered: bool = False) -> bool:
        """Book one broker fill into the ledger, once per trade ID. Returns False for a repeat."""
        trade = str(ev.trade_id)
        if trade in self._applied_set:
            return False
        qty, px = float(ev.last_qty), float(ev.last_px)
        fee = float(ev.commission.as_double()) if ev.commission is not None else 0.0
        buy = ev.order_side == OrderSide.BUY
        ticker = str(ev.instrument_id.symbol)
        self.cash += -qty * px - fee if buy else qty * px - fee
        self.commissions += fee
        self.held[ticker] = self.held.get(ticker, 0.0) + (qty if buy else -qty)
        self.applied.append(trade)
        self._applied_set.add(trade)
        if len(self.applied) > 2 * _KEEP_TRADE_IDS:
            self.applied = self.applied[-_KEEP_TRADE_IDS:]
            self._applied_set = set(self.applied)
        self._save()
        self.publish_message(T_FILL, FillReport(ev.ts_event, ticker, "BUY" if buy else "SELL", qty, px, fee))
        if recovered:
            self._risk_event(
                "fill_recovered",
                f"{ticker} {'BUY' if buy else 'SELL'} {qty:g} @ {px:.2f} (trade {trade}) filled while the node was down; "
                "booked from the broker's report",
            )
        return True

    def _close_pending(self, client_order_id) -> None:
        if self.pending.pop(str(client_order_id), None) is not None:
            self._save()

    def _recover_pending(self) -> None:
        """After a restart: settle every journaled order from what the broker reported at reconciliation."""
        for coid, info in list(self.pending.items()):
            order = self.cache.order(ClientOrderId(coid))
            if order is None:
                # The broker reports no such order: it never arrived (crash between journal and send),
                # or was rejected before acceptance. Nothing was traded, so the journal entry goes.
                self._risk_event("order_unknown", f"{info['ticker']}: order {coid} unknown to the broker; dropped")
                self._close_pending(coid)
                continue
            for e in order.events():
                if type(e).__name__ == "OrderFilled":
                    self._apply_fill(e, recovered=True)
            if order.is_closed:
                self._close_pending(coid)

    def on_order_filled(self, ev):
        coid = str(ev.client_order_id)
        if self.config.live and coid not in self.pending:
            # Not an order this agent journaled (e.g. a trade placed by hand in the same paper
            # account): it is not part of the lab's book, and reconciliation will flag the difference.
            self._risk_event(
                "foreign_fill", f"{ev.instrument_id}: fill of order {coid} not sent by the lab; not booked"
            )
            return
        self._apply_fill(ev)
        order = self.cache.order(ev.client_order_id)
        if order is not None and order.is_closed:
            self._close_pending(ev.client_order_id)

    def on_order_canceled(self, ev):
        self._close_pending(ev.client_order_id)

    def on_order_rejected(self, ev):
        self.rejections += 1
        self._close_pending(ev.client_order_id)
        self._risk_event("order_rejected", f"{ev.instrument_id}: {ev.reason}")

    def on_order_denied(self, ev):
        self.rejections += 1
        self._close_pending(ev.client_order_id)
        self._risk_event("order_denied", f"{ev.instrument_id}: {ev.reason}")

    def on_order_expired(self, ev):
        self._close_pending(ev.client_order_id)
        self._risk_event("order_expired", f"{ev.instrument_id}: order expired unfilled")
