"""ExecutionAgent: the only component allowed to send orders."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nautilus_trader.model import OrderSide, TimeInForce
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
from lab.persistence import atomic_json_write
from lab.safety import is_paper_account, read_kill_switch

NY = ZoneInfo("America/New_York")


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
        expected_sleeves: tuple[str, ...] = (),
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
        self.expected_sleeves = tuple(expected_sleeves)


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
        self.sleeve_timestamps: dict[str, int] = {}
        self.approved_timestamp = 0
        # The lab's own ledger: capital - purchases + sales - commissions, and the shares
        # it believes it holds. Independent of anything else in the broker account.
        self.cash = config.capital
        self.commissions = 0.0
        self.held: dict[str, float] = dict.fromkeys(config.instruments, 0.0)
        self.unresolved_orders: dict[str, dict] = {}
        self.daily_usage = {"session": "", "orders": 0, "bought": 0.0}
        if config.state_path and Path(config.state_path).exists():
            state = json.loads(Path(config.state_path).read_text())
            self.cash = float(state["cash"])
            self.commissions = float(state.get("commissions", 0.0))
            self.held.update({t: float(q) for t, q in state.get("positions", {}).items() if t in self.held})
            self.unresolved_orders = dict(state.get("unresolved_orders", {}))
            self.daily_usage = dict(state.get("daily_usage", self.daily_usage))
            if (
                not isinstance(self.unresolved_orders, dict)
                or not isinstance(self.daily_usage, dict)
                or not {"session", "orders", "bought"} <= self.daily_usage.keys()
            ):
                raise ValueError("invalid persisted execution state; refusing to start")

    # ------------------------------------------------------------------ lifecycle

    def on_start(self):
        self.subscribe_topic(T_SIGNAL, self._on_signal)
        self.subscribe_topic(T_APPROVED, self._on_approved)
        if self.config.live:
            now = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY)
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
        self.sleeve_timestamps[msg.sleeve] = msg.ts
        self.prices[msg.ticker] = msg.price
        self.price_ts = max(self.price_ts, msg.ts)
        self.new_data = True
        if not self.alert_pending:
            self._schedule(datetime.fromtimestamp(msg.ts / 1e9, NY))

    def _on_approved(self, msg):
        self.approved_timestamp = max(self.approved_timestamp, msg.ts)
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
        if self.new_data or self.config.live:
            self.new_data = False
            self.rebalance()
        self._schedule(datetime.fromtimestamp(event.ts_event / 1e9, NY))

    def _on_heartbeat(self, _event):
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
                        "accounts": [str(a.id) for a in self.cache.accounts()],
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
        # An explicit operator recovery is only safe after all broker orders are terminal.
        for iid in self.config.instruments.values():
            if self.cache.orders_open_count(instrument_id=iid) > 0:
                raise RuntimeError("cannot adopt positions while broker orders remain open")
        self.held = dict.fromkeys(self.config.instruments, 0.0)
        self.cash = self.config.capital
        self.unresolved_orders.clear()
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
            atomic_json_write(
                p,
                {
                    "cash": self.cash,
                    "commissions": self.commissions,
                    "positions": self._nonzero(self.held),
                    "unresolved_orders": self.unresolved_orders,
                    "daily_usage": self.daily_usage,
                    "updated": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )

    def _kill(self) -> dict | None:
        return read_kill_switch(self.config.kill_path) if self.config.kill_path else None

    def _risk_event(self, kind: str, detail: str):
        self.publish_message(T_RISK, RiskEvent(self.clock.timestamp_ns(), kind, detail))

    def _cancel_open_orders(self):
        for iid in self.config.instruments.values():
            if self.cache.orders_open_count(instrument_id=iid) > 0:
                self.cancel_all_orders(iid)

    # ------------------------------------------------------------------ the daily decision

    def _missing_session_signals(self, session):
        """Require EVERY configured sleeve to report a bar for today's session."""
        missing = [
            sleeve
            for sleeve in self.config.expected_sleeves
            if sleeve not in self.sleeve_timestamps
            or datetime.fromtimestamp(self.sleeve_timestamps[sleeve] / 1e9, NY).date() != session
        ]
        if self.config.expected_sleeves and (
            not self.approved_timestamp
            or datetime.fromtimestamp(self.approved_timestamp / 1e9, NY).date() != session
        ):
            missing.append("risk approval")
        return missing

    def rebalance(self):
        session = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY).date()
        if self.config.live:
            missing = self._missing_session_signals(session)
            if missing:
                self.blocked_reason = f"session {session}: missing/stale signals: {', '.join(missing)}"
                self._risk_event("data_incomplete", self.blocked_reason)
                return
            if self.unresolved_orders:
                self.blocked_reason = f"unresolved order intents: {list(self.unresolved_orders)}"
                self._risk_event("unresolved_orders", self.blocked_reason + "; reconcile orders before resuming")
                return
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
            orders.append((diff, iid, ticker, price))

        orders.sort(key=lambda o: o[0] > 0)  # sells first
        session_key = session.isoformat()
        if self.daily_usage["session"] != session_key:
            self.daily_usage = {"session": session_key, "orders": 0, "bought": 0.0}
            self._save()
        for diff, iid, ticker, price in orders:
            if diff > 0 and diff * price > self.config.max_order_value:
                diff = math.floor(self.config.max_order_value / price)
                self._risk_event("order_capped", f"{ticker} buy capped at {self.config.max_order_value:,.0f} USD")
                if diff <= 0:
                    continue
            if self.daily_usage["orders"] >= self.config.max_orders_per_day:
                self._risk_event("daily_cap", f"max {self.config.max_orders_per_day} orders/day reached; rest deferred")
                break
            if diff > 0 and (self.daily_usage["bought"] + diff * price) / snap.equity > self.config.max_daily_turnover:
                self._risk_event(
                    "daily_cap", f"daily buy cap {self.config.max_daily_turnover:.0%} of equity reached; rest deferred"
                )
                break
            self._submit(iid, ticker, diff, price)

    def _submit(self, iid, ticker: str, diff: float, reference_price: float) -> bool:
        if self.config.live and not self._paper_guard():
            return False
        inst = self.cache.instrument(iid)
        if inst is None:
            self._risk_event("blocked", f"{ticker}: instrument {iid} not loaded; order skipped")
            return False
        side = OrderSide.BUY if diff > 0 else OrderSide.SELL
        tif = TimeInForce.AT_THE_OPEN if self.config.live else TimeInForce.GTC
        order = self.order_factory.market(iid, side, inst.make_qty(abs(diff)), time_in_force=tif)
        # Write ahead of submission. A crash after recording but before broker ACK
        # must block the next session rather than risk a duplicate order.
        oid = str(order.client_order_id)
        self.unresolved_orders[oid] = {
            "ticker": ticker,
            "side": str(side),
            "quantity": abs(diff),
            "filled": 0.0,
        }
        self.daily_usage["orders"] += 1
        if diff > 0:
            self.daily_usage["bought"] += diff * reference_price
        self._save()
        try:
            self.submit_order(order)
        except Exception as exc:
            self._risk_event("submit_uncertain", f"{ticker} {oid}: {exc}; manual broker reconciliation required")
            raise
        self.orders_sent += 1
        return True

    def _paper_guard(self) -> bool:
        ids = [str(a.id) for a in self.cache.accounts()]
        expected = self.config.paper_account
        ok = bool(ids) and all(is_paper_account(i) and i.endswith(expected) for i in ids)
        if not ok:
            msg = f"refusing to trade: broker accounts {ids} are not the configured paper account {expected!r}"
            self.log.error(msg)
            self._risk_event("paper_guard", msg)
            self.shutdown_system(msg)
        return ok

    # ------------------------------------------------------------------ order events

    def on_order_filled(self, ev):
        qty, px = float(ev.last_qty), float(ev.last_px)
        fee = float(ev.commission.as_double())
        buy = ev.order_side == OrderSide.BUY
        ticker = str(ev.instrument_id.symbol)
        self.cash += -qty * px - fee if buy else qty * px - fee
        self.commissions += fee
        self.held[ticker] = self.held.get(ticker, 0.0) + (qty if buy else -qty)
        oid = str(ev.client_order_id)
        if oid in self.unresolved_orders:
            pending = self.unresolved_orders[oid]
            pending["filled"] += qty
            if pending["filled"] >= pending["quantity"] - 1e-9:
                del self.unresolved_orders[oid]
        self._save()
        self.publish_message(T_FILL, FillReport(ev.ts_event, ticker, "BUY" if buy else "SELL", qty, px, fee))

    def _finish_order(self, ev):
        oid = str(ev.client_order_id)
        if oid in self.unresolved_orders:
            del self.unresolved_orders[oid]
            self._save()

    def on_order_canceled(self, ev):
        self._finish_order(ev)

    def on_order_rejected(self, ev):
        self._finish_order(ev)
        self.rejections += 1
        self._risk_event("order_rejected", f"{ev.instrument_id}: {ev.reason}")

    def on_order_denied(self, ev):
        self._finish_order(ev)
        self.rejections += 1
        self._risk_event("order_denied", f"{ev.instrument_id}: {ev.reason}")

    def on_order_expired(self, ev):
        self._finish_order(ev)
        self._risk_event("order_expired", f"{ev.instrument_id}: order expired unfilled")
