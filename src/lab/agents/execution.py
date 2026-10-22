"""ExecutionAgent: the only component allowed to send orders."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nautilus_trader.model import ClientOrderId, OrderSide, TimeInForce
from nautilus_trader.trading import Strategy, StrategyConfig

from lab import margin as mg
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
from lab.config import MarginSettings
from lab.halt import read_halt, set_halt
from lab.safety import is_paper_account, read_kill_switch

NY = ZoneInfo("America/New_York")
# Hard halts under which exits are still sent (everything else in a halt sends nothing at all).
_REDUCE_ONLY_HALTS = {"circuit_breaker", "margin_call"}
_KEEP_TRADE_IDS = 2000  # broker trade IDs remembered for de-duplication (far more than a few days of fills)


def _opening(current: float, target: float) -> float:
    """Shares of an order that add exposure (a new or larger long or short); the rest only reduces it."""
    if current == 0 or current * target < 0:
        return abs(target)
    return max(0.0, abs(target) - abs(current))


def _with_opening(current: float, target: float, opening: float) -> float:
    """The target that keeps the order's direction but adds only `opening` shares of new exposure."""
    side = math.copysign(1.0, target)
    base = abs(current) if current * target > 0 else 0.0
    return side * (base + opening)


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
        halt_path: str | None = None,
        adopt_path: str | None = None,
        heartbeat_path: str | None = None,
        adopt_broker_positions: bool = False,
        weekdays_only: bool = True,
        margin: MarginSettings | None = None,
        **_kwargs,
    ):
        """instruments: ticker -> InstrumentId. margin: financing rates accrued on short and levered books.

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
        self.halt_path = halt_path  # hard-halt file (lab/halt.py)
        self.adopt_path = adopt_path  # request file written by `lab recover --adopt-broker`
        self.heartbeat_path = heartbeat_path
        self.adopt_broker_positions = adopt_broker_positions
        self.weekdays_only = weekdays_only
        self.margin = margin or MarginSettings()


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
        self.price_stamps: dict[str, int] = {}
        self.price_ts = 0
        self.new_data = False
        self.alert_pending = False
        self.halted = False
        self.reduce_only = False
        self.blocked_reason = ""
        self.rejections = 0
        self.orders_sent = 0
        self.min_cash = math.inf
        # The lab's own ledger: capital - purchases + sales - commissions, and the shares
        # it believes it holds. Independent of anything else in the broker account.
        self.cash = config.capital
        self.commissions = 0.0
        # Borrow fees and debit interest accrued (estimated with lab.toml [margin] rates), deducted from equity.
        # Kept apart from cash so cash stays exactly what the broker's trades imply.
        self.financing = 0.0
        self.borrow_fees = 0.0
        self.accrued_to = 0  # bar timestamp the financing has been accrued up to
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
            self.financing = float(state.get("financing", 0.0))
            self.borrow_fees = float(state.get("borrow_fees", 0.0))
            self.accrued_to = int(state.get("accrued_to", 0))
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
            previous = self._read_heartbeat()
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
            if previous and previous.get("running"):
                # The last run never stopped cleanly (crash, container or gateway restart): resumed by itself.
                self._risk_event(
                    "auto_resumed",
                    f"restarted after an unclean stop (last heartbeat {previous.get('at')}); start-up checks "
                    "passed, trading resumes"
                    if not self._halt()
                    else f"restarted after an unclean stop (last heartbeat {previous.get('at')}); a hard halt is active",
                )
            account = self._account_id()
            if account is not None and not self._paper_guard():
                return
            halt = self._halt()
            if halt:
                self._risk_event(
                    "hard_halt",
                    f"hard halt active since {halt.get('at')}: {halt['kind']}: {halt.get('reason', '')}. "
                    "No orders until an operator runs `lab recover --reason ...`",
                )

    def on_stop(self):
        if self.config.live:
            self._write_heartbeat(running=False)

    def _on_signal(self, msg):
        self.prices[msg.ticker] = msg.price
        self.price_stamps[msg.ticker] = msg.ts
        self.price_ts = max(self.price_ts, msg.ts)
        self.new_data = True
        if not self.alert_pending:
            self._schedule(datetime.fromtimestamp(msg.ts / 1e9, NY))

    def _on_approved(self, msg):
        self.weights.update(msg.weights)
        self.halted = msg.halted  # follows the RiskAgent: halted until its breaker is cleared
        self.reduce_only = msg.reduce_only

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
        elif self.config.live:
            # A transient condition: nothing to clear, the next session's data resumes trading.
            self._risk_event(
                "stale_data",
                "no new daily bar since the last rebalance (market holiday or data delay); no orders today",
            )
        self._schedule(datetime.fromtimestamp(event.ts_event / 1e9, NY))

    def _on_heartbeat(self, _event):
        if self.pending:
            self._recover_pending()  # safety net: settle journaled orders from the broker's latest state
        if self._kill():
            self._cancel_open_orders()
        self._write_heartbeat(running=True)

    def _read_heartbeat(self) -> dict | None:
        p = Path(self.config.heartbeat_path) if self.config.heartbeat_path else None
        if p is None or not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {"running": True, "at": "unreadable"}

    def _write_heartbeat(self, *, running: bool):
        if not self.config.heartbeat_path:
            return
        kill = self._kill()
        halt = self._halt()
        p = Path(self.config.heartbeat_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "running": running,  # False only after a clean stop: a restart then is not an auto-resume
                    "cash": round(self.cash, 2),
                    "positions": self._nonzero(self.held),
                    "pending_orders": len(self.pending),
                    "kill_switch": kill["mode"] if kill else None,
                    "hard_halt": halt["kind"] if halt else None,
                    "halted": self.halted,
                    "blocked": self.blocked_reason or None,
                    "orders_sent": self.orders_sent,
                    "account": self._account_id(),
                }
            )
        )
        tmp.replace(p)

    def _halt(self) -> dict | None:
        return read_halt(self.config.halt_path) if self.config.halt_path else None

    def _hard_halt(self, kind: str, reason: str):
        if self.config.halt_path:
            set_halt(self.config.halt_path, kind, reason)
        self._risk_event("hard_halt", f"{kind}: {reason}. No orders until `lab recover --reason ...`")

    # ------------------------------------------------------------------ state

    @staticmethod
    def _nonzero(d: dict) -> dict:
        return {k: v for k, v in d.items() if v}

    def account_cash(self) -> float:
        """Cash according to the broker/simulator account (cross-check for the ledger).

        A cash account's balance is its cash. A margin account's balance is capital plus realized P&L, so its
        cash is that balance minus the cost basis (signed quantity x average open price) of the open positions."""
        acct = self.cache.account_for_venue(self.config.venue)
        bal = acct.balance_total() if acct is not None else None
        if bal is None:
            return float("nan")
        cash = float(bal.as_double())
        if "MARGIN" in str(getattr(acct, "account_type", "")):
            for iid in self.config.instruments.values():
                for pos in self.cache.positions_open(instrument_id=iid):
                    avg = pos.avg_px_open() if callable(pos.avg_px_open) else pos.avg_px_open
                    cash -= float(pos.signed_qty) * float(avg)
        return cash

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

    def _book(self) -> tuple[float, float]:
        long_value = sum(q * self.prices.get(t, 0.0) for t, q in self.held.items() if q > 0)
        short_value = -sum(q * self.prices.get(t, 0.0) for t, q in self.held.items() if q < 0)
        return long_value, short_value

    def _accrue(self) -> None:
        """Charge borrow fees and debit interest for the calendar days since the last accrual."""
        ts = self.price_ts
        if self.accrued_to and ts > self.accrued_to:
            days = round((ts - self.accrued_to) / 86_400e9)
            if days > 0:
                _, short_value = self._book()
                borrow, debit = mg.financing_cost(self.cash, short_value, days, self.config.margin)
                self.borrow_fees += borrow
                self.financing += borrow + debit
        if ts and ts > self.accrued_to:
            self.accrued_to = ts
            self._save()  # persist every accrual: a restart must not re-accrue the span at a different book

    def snapshot(self) -> EquitySnapshot:
        self._accrue()
        long_value, short_value = self._book()
        equity = self.cash + long_value - short_value - self.financing
        self.min_cash = min(self.min_cash, self.cash)
        gross = (long_value + short_value) / equity if equity > 0 else 0.0
        net = (long_value - short_value) / equity if equity > 0 else 0.0
        return EquitySnapshot(
            self.price_ts,
            equity,
            self.cash,
            dict(self.held),
            gross,
            net=net,
            long_value=long_value,
            short_value=short_value,
            financing=self.financing,
        )

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
                        "financing": self.financing,
                        "borrow_fees": self.borrow_fees,
                        "accrued_to": self.accrued_to,
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

    def _unsafe_held_marks(self) -> list[str]:
        """No new opening trade is safe if the held book cannot be marked consistently."""
        if not self.config.live:
            return []
        stale = []
        for ticker, qty in self.held.items():
            if not qty:
                continue
            mark = self.prices.get(ticker)
            ts = self.price_stamps.get(ticker)
            if (
                mark is None
                or not math.isfinite(mark)
                or mark <= 0
                or not self.price_ts
                or ts != self.price_ts
            ):
                stale.append(ticker)
        return stale

    def rebalance(self):
        unsafe = self._unsafe_held_marks()
        if unsafe:
            self.blocked_reason = f"missing/stale marks for held positions: {', '.join(sorted(unsafe))}"
            self._risk_event("stale_data", self.blocked_reason + "; no new orders until all held marks are current")
            return
        snap = self.snapshot()
        self.publish_message(T_EQUITY, snap)  # RiskAgent may halt here; its reply arrives synchronously
        if not math.isfinite(snap.equity) or snap.equity <= 0:
            self._risk_event("blocked", f"cannot size orders: equity={snap.equity}")
            return

        if self.config.adopt_path and Path(self.config.adopt_path).exists():
            # The operator chose the broker's positions as the truth (lab recover --adopt-broker).
            self._adopt_broker_positions()
            Path(self.config.adopt_path).unlink()
            snap = self.snapshot()

        halt = self._halt()
        if halt and halt["kind"] not in _REDUCE_ONLY_HALTS:
            self.blocked_reason = f"hard halt: {halt['kind']}"
            self._risk_event("hard_halt", f"{halt['kind']} since {halt.get('at')}: no orders (lab recover clears it)")
            return

        diffs = self.reconcile()
        if diffs:
            self.blocked_reason = "ledger and broker disagree: " + "; ".join(diffs)
            self._risk_event(
                "reconciliation_mismatch",
                self.blocked_reason + ". Fill recovery could not explain it. Check the broker account, then "
                "`lab recover --reason ... --adopt-broker` to take the broker's positions as the truth.",
            )
            if self.config.live:
                self._hard_halt("reconciliation_mismatch", self.blocked_reason)
            return
        self.blocked_reason = ""

        kill = self._kill()
        flatten = bool(kill and kill.get("mode") == "flatten")
        if kill and not flatten:
            self._cancel_open_orders()
            self._risk_event("kill_switch", f"halted by kill switch ({kill.get('reason', '')}); no orders sent")
            return

        reduce_only = self.reduce_only or (halt is not None and halt["kind"] == "margin_call")
        orders = []
        for ticker, iid in self.config.instruments.items():
            price = self.prices.get(ticker)
            if not price:
                continue
            current = self.held.get(ticker, 0.0)
            # A breaker halt, the kill switch's flatten or a halted RiskAgent exit everything.
            exit_all = self.halted or flatten or (halt is not None and not reduce_only)
            w = 0.0 if exit_all else self.weights.get(ticker, 0.0)
            target = math.trunc(w * snap.equity * (1 - self.config.cash_buffer) / price)  # signed, toward zero
            if reduce_only:
                # Margin call: exposure may only shrink, never flip or grow.
                target = 0 if target * current <= 0 else math.copysign(min(abs(target), abs(current)), current)
            diff = target - current
            if diff == 0:
                continue
            value = abs(diff) * price
            if target != 0 and value < self.config.min_trade_value:
                continue
            if (
                target != 0
                and current != 0
                and target * current > 0
                and value < self.config.rebalance_band * snap.equity
            ):
                continue  # small drift: not worth the commission
            if self.cache.orders_open_count(instrument_id=iid) > 0:
                continue
            if any(p["ticker"] == ticker for p in self.pending.values()):
                # An earlier order for this stock is not settled yet: never risk a duplicate.
                self._risk_event("order_pending", f"{ticker}: previous order still unsettled; no new order today")
                continue
            orders.append((_opening(current, target), target, current, iid, ticker, price))

        orders.sort(key=lambda o: o[0] > 0)  # pure reductions first: they free margin and are never capped
        sent = 0
        opened = 0.0
        for opening, target, current, iid, ticker, price in orders:
            if opening > 0 and opening * price > self.config.max_order_value:
                cap = math.floor(self.config.max_order_value / price)
                target = _with_opening(current, target, cap)
                opening = cap
                self._risk_event(
                    "order_capped", f"{ticker}: new exposure capped at {self.config.max_order_value:,.0f} USD"
                )
            if sent >= self.config.max_orders_per_day:
                self._risk_event("daily_cap", f"max {self.config.max_orders_per_day} orders/day reached; rest deferred")
                break
            if opening > 0 and (opened + opening * price) / snap.equity > self.config.max_daily_turnover:
                self._risk_event(
                    "daily_cap",
                    f"daily cap on new exposure {self.config.max_daily_turnover:.0%} of equity reached; rest deferred",
                )
                break
            diff = target - current
            if diff != 0 and self._submit(iid, ticker, diff):
                sent += 1
                opened += opening * price

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
            if self.config.halt_path:
                set_halt(self.config.halt_path, "paper_guard", msg)
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
                # Absence from the reconciliation cache is NOT proof the order never
                # reached IBKR. A crash may occur after submission but before any
                # acknowledgment, and broker reports may be incomplete/delayed.
                # Keep the write-ahead intent so a delayed fill remains attributable.
                # Do not issue a replacement order until the uncertainty is resolved.
                detail = (
                    f"{info['ticker']}: journaled order {coid} not confirmed by broker reconciliation; "
                    "possible in-flight order. Preserve intent and require audited investigation"
                )
                self._risk_event("order_unknown", detail)
                halt = self._halt()
                if halt is None or halt.get("kind") != "reconciliation_mismatch":
                    self._hard_halt("reconciliation_mismatch", detail)
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
