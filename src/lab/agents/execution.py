"""ExecutionAgent: the only component allowed to send orders."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nautilus_trader.model import ClientOrderId, OrderSide, TimeInForce
from nautilus_trader.trading import Strategy, StrategyConfig

from lab import margin as mg
from lab.agents.messages import (
    T_APPROVED,
    T_EQUITY,
    T_FILL,
    T_MARK,
    T_RISK,
    T_SIGNAL,
    EquitySnapshot,
    FillReport,
    RiskEvent,
)
from lab.agents.schedule import MAX_BAR_AGE_DAYS, bar_freshness, market_now, next_at
from lab.borrow import BorrowBook, BorrowUnavailable, HistoricalBorrowLedger
from lab.config import MarginSettings
from lab.execution_state import validate_ledger
from lab.halt import halt_kinds, read_halt, set_halt
from lab.persistence import durable_unlink, read_json
from lab.safety import is_paper_account, read_kill_switch

NY = ZoneInfo("America/New_York")
# Hard halts under which exits are still sent (everything else in a halt sends nothing at all).
_REDUCE_ONLY_HALTS = {"circuit_breaker", "margin_call", "cash_mismatch"}
# Provisional retention after receipt, not a qualified broker replay horizon.
# Pending/disposed orders and legacy IDs without order attribution must not expire.
_KEEP_TRADE_DAYS = 30


def _ny_date(ts_ns: int):
    return datetime.fromtimestamp(ts_ns / 1e9, NY).date()


def _opening(current: float, target: float) -> float:
    """Shares of an order that add exposure (a new or larger long or short); the rest only reduces it."""
    if current == 0 or current * target < 0:
        return abs(target)
    return max(0.0, abs(target) - abs(current))


def _liquidity_cap(seen, participation: float | None) -> float | None:
    """Shares of new exposure one order may add: `participation` x the average volume of the bars `seen`
    ((ts, volume) pairs). None (no cap) without the setting or before five bars of volume are known."""
    if not participation or not seen or len(seen) < 5:
        return None
    return math.floor(participation * sum(v for _, v in seen) / len(seen))


def _held_breach(held: dict, prices: dict, equity: float, limits, margin) -> str | None:
    """Which hard limit (per stock, gross, short book, net, initial margin) the held book breaks at the
    current marks, or None. Without risk limits (component tests) nothing is checked."""
    if limits is None or not any(held.values()):
        return None
    from lab.reservations import check_openings

    reason = check_openings(held, {}, prices, equity, limits, margin or MarginSettings())
    return reason.replace("possible fill exceeds", "held book exceeds") if reason else None


def _exposure_note(held: dict, prices: dict, equity: float, margin) -> str:
    """The book's exposure at the decision marks, for breach reports (G013-F2)."""
    if equity <= 0:
        return "no positive equity"
    w = {t: q * prices.get(t, 0.0) / equity for t, q in held.items() if q}
    return (
        f"gross {sum(abs(v) for v in w.values()):.4f}, net {sum(w.values()):.4f}, "
        f"short {-sum(min(v, 0) for v in w.values()):.4f}, "
        f"largest {max((abs(v) for v in w.values()), default=0):.4f}, "
        f"initial margin {mg.initial_requirement(w, margin or MarginSettings()):.4f} of equity"
    )


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
        dispose_path: str | None = None,
        heartbeat_path: str | None = None,
        adopt_broker_positions: bool = False,
        weekdays_only: bool = True,
        margin: MarginSettings | None = None,
        borrow_book: BorrowBook | None = None,
        dedicated_account: bool = False,
        market_offset_days: float = 0.0,
        max_bar_age_days: float = MAX_BAR_AGE_DAYS,
        cash_tolerance: float = 1.0,
        max_external_cash: float = 0.02,
        session=None,
        flat_at_close: bool = False,
        decision_delay_s: float = 1.0,
        limit_collar: float = 0.01,
        max_gross: float | None = None,
        risk_limits=None,
        authority=None,
        notification_health=None,
        reservation_costs=None,
        reservation_gap: float = 0.0,
        max_participation: float | None = None,
        participation_window: int = 20,
        entry_slippage: float = 0.0,
        **_kwargs,
    ):
        """instruments: ticker -> InstrumentId. margin: financing rates accrued on short and levered books.

        max_order_value:    no single buy larger than this (USD)
        max_orders_per_day: orders per session day (all rebalances of the day); past it, only reductions go out
        max_daily_turnover: new exposure opened per session day / equity (1.0 = the whole portfolio)
        session:            None = daily (rebalance at rebalance_time after the close). A quick speed
                            (agents.schedule.Session): rebalance after each bar; backtest decision_delay_s after
                            the bar reaches the agents, live the session's decide_s after the bar is requested
        flat_at_close:      quick speeds: exit everything in the session's last interval and open nothing new
                            until the next session (no overnight gap risk, no overnight financing)
        limit_collar:       quick speeds, live: new exposure goes out as a limit order this far through the
                            last (delayed) price, cancelled if still working a bar later; exits are market orders
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
        self.dispose_path = dispose_path  # request file written by `lab recover --dispose-order ... --evidence ...`
        self.heartbeat_path = heartbeat_path
        self.adopt_broker_positions = adopt_broker_positions
        self.weekdays_only = weekdays_only
        self.margin = margin or MarginSettings()
        self.dedicated_account = dedicated_account
        self.market_offset_days = market_offset_days  # harness only (agents/schedule.market_now)
        self.max_bar_age_days = max_bar_age_days
        self.cash_tolerance = cash_tolerance
        self.max_external_cash = max_external_cash
        self.session = session
        self.flat_at_close = flat_at_close
        self.decision_delay_s = decision_delay_s
        self.limit_collar = limit_collar
        # The risk limit on gross exposure (fraction of equity): new exposure sent beside unfilled reductions
        # must fit under it with the whole current book. None: nothing is added until reductions settle.
        self.max_gross = max_gross
        self.risk_limits = risk_limits
        self.notification_health = notification_health
        self.authority = authority
        self.reservation_costs = reservation_costs
        self.max_participation = max_participation
        self.participation_window = participation_window
        self.reservation_gap = reservation_gap  # adverse move stressed for cash and margin
        self.entry_slippage = entry_slippage  # worst entry price assumed for exposure caps
        if live and borrow_book is not None:
            raise ValueError("historical borrow evidence cannot authorize live paper-broker orders")
        if state_path and borrow_book is not None:
            raise ValueError("historical borrow runs require a fresh ledger, not persisted paper state")
        self.borrow_book = borrow_book


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
        self.authority_nonce = None
        self.weights: dict[str, float] = {}
        self.prices: dict[str, float] = {}
        self.volumes: dict[str, deque] = {}  # ticker -> recent (bar ts, bar volume): the liquidity cap
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
        self.external_cash = 0.0  # dedicated account: cash moves booked from the broker (dividends, interest)
        self.synced_cash = False  # dedicated account: ledger cash has matched the broker's at least once
        self.accrued_to = 0  # bar timestamp the financing has been accrued up to
        self.borrow_ledger = HistoricalBorrowLedger(config.borrow_book) if config.borrow_book is not None else None
        self.borrow_error: str | None = None
        # Daily caps count every rebalance of a session day (quick speeds rebalance many times a day).
        self.cap_day = None
        self.cap_orders = 0
        self.cap_opened = 0.0
        self.flattened_day = None
        self.liquidation_requested = False
        self.exit_retry_pending = False
        self.held: dict[str, float] = dict.fromkeys(config.instruments, 0.0)
        # Write-ahead order journal: every order is recorded here BEFORE it is sent, and stays
        # until the broker reports it closed. After a crash between order and fill, the fills are
        # recovered from the broker's reports at start-up (_recover_pending) instead of leaving the
        # ledger behind the broker. Fills are applied once per broker trade ID.
        self.pending: dict[str, dict] = {}
        # Unconfirmed intents an operator disposed of with broker evidence (`lab recover --dispose-order`). Kept,
        # not erased: a fill that still arrives for one is booked to the lab and halts for review.
        self.disposed: dict[str, dict] = {}
        self.terminal: dict[str, dict] = {}  # retain attribution for cancel/fill races and late broker reports
        self.persistence_failed = False
        self.orphans: dict[str, float] = {}
        self.ledger_account: str | None = None  # the broker account this ledger belongs to
        self.applied: dict[str, list] = {}  # trade ID -> [fill time ns, client order ID]
        from lab.execution_store import ExecutionStore

        self.store = ExecutionStore(config.state_path) if config.state_path else None
        state = self.store.load() if self.store else None
        if state is not None:
            validate_ledger(state)
            self.cash = float(state["cash"])
            self.commissions = float(state.get("commissions", 0.0))
            self.financing = float(state.get("financing", 0.0))
            self.external_cash = float(state.get("external_cash", 0.0))
            self.synced_cash = bool(state.get("synced_cash", False))
            self.borrow_fees = float(state.get("borrow_fees", 0.0))
            self.accrued_to = int(state.get("accrued_to", 0))
            self.held.update({t: float(q) for t, q in state.get("positions", {}).items() if t in self.held})
            # Positions in stocks no longer configured are never dropped silently (G007-F15): start-up halts.
            self.orphans = {t: float(q) for t, q in state.get("positions", {}).items() if t not in self.held and q}
            self.ledger_account = state.get("account")
            self.pending = dict(state.get("pending", {}))
            self.disposed = dict(state.get("disposed", {}))
            self.terminal = dict(state.get("terminal", {}))
            now = time.time_ns()
            for entry in state.get("applied_trades", []):
                if isinstance(entry, str):  # legacy IDs have no order attribution: retain them
                    self.applied[entry] = [now, None]
                elif isinstance(entry, list) and len(entry) == 3 and isinstance(entry[0], str):
                    self.applied[entry[0]] = [int(entry[1]), entry[2]]
                else:
                    raise ValueError("invalid applied trade record")
            caps = state.get("daily_caps") or {}
            self.cap_day = date.fromisoformat(caps["day"]) if caps.get("day") else None
            self.cap_orders = int(caps.get("orders", 0))
            self.cap_opened = float(caps.get("opened", 0.0))
            numbers = [
                self.cash,
                self.commissions,
                self.financing,
                self.external_cash,
                self.borrow_fees,
                self.cap_opened,
                *self.held.values(),
                *self.orphans.values(),
            ]
            if not all(math.isfinite(v) for v in numbers) or self.cap_orders < 0:
                raise ValueError("invalid/nonfinite execution ledger")
            for intent in self.pending.values():
                if (
                    not isinstance(intent, dict)
                    or intent.get("side") not in ("BUY", "SELL")
                    or not isinstance(intent.get("ticker"), str)
                ):
                    raise ValueError("invalid pending order intent")
                if not math.isfinite(float(intent.get("qty", 0))) or float(intent.get("qty", 0)) <= 0:
                    raise ValueError("invalid pending order quantity")

        if self.store:
            self.terminal = self.store.maps["terminal"]
            self.disposed = self.store.maps["disposed"]
            self.applied = self.store.maps["applied"]

    # ------------------------------------------------------------------ lifecycle

    def on_start(self):
        self.subscribe_topic(T_SIGNAL, self._on_signal)
        self.subscribe_topic(T_APPROVED, self._on_approved)
        if self.config.max_participation:
            self.subscribe_topic(T_MARK, self._on_mark)
        if self.config.live:
            now = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY)
            previous = self._read_heartbeat()
            if self.config.adopt_broker_positions:
                self._adopt_broker_positions()
            self._consume_adopt()
            self._consume_dispose()
            self._recover_pending()
            if self.config.session is None:
                self._schedule(now)  # daily; a quick speed decides after every bar instead
            elif self.config.flat_at_close:
                self._schedule_session_exit(now)
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
            if account is not None and self.ledger_account not in (None, account):
                self._hard_halt(
                    "reconciliation_mismatch",
                    f"this ledger belongs to account {self.ledger_account}, the broker reports {account}",
                )
            elif account is not None and self.ledger_account is None:
                self.ledger_account = account
                self._save()
            if self.orphans:
                self._hard_halt(
                    "reconciliation_mismatch",
                    f"the ledger holds {self.orphans} in stocks that are not in this node's universe",
                )
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

    def _on_mark(self, msg):
        """Bar volumes, warm-up included, for the liquidity cap (Gemini audit F-R06)."""
        if msg.volume > 0 and msg.ticker in self.config.instruments:
            seen = self.volumes.setdefault(msg.ticker, deque(maxlen=self.config.participation_window))
            if not seen or seen[-1][0] < msg.ts:  # several agents may report the same bar
                seen.append((msg.ts, msg.volume))

    def _on_signal(self, msg):
        self.prices[msg.ticker] = msg.price
        self.price_stamps[msg.ticker] = msg.ts
        self.price_ts = max(self.price_ts, msg.ts)
        self.new_data = True
        if self.alert_pending:
            return
        if self.config.session is not None:
            # Quick speed: decide just after the bar arrives. Backtest: every stock's bar arrives at the same
            # moment. Live: the session's decide_s after this cycle's request, when every response is in.
            now = self.clock.timestamp_ns()
            at = now + int(self.config.decision_delay_s * 1e9)
            if self.config.live:
                sess = self.config.session
                t = datetime.fromtimestamp(now / 1e9, NY)
                fetched = [f for f in sess.fetch_times(t) if f <= t]
                if fetched:
                    at = max(at, int((fetched[-1] + timedelta(seconds=sess.decide_s)).timestamp() * 1e9))
            self.clock.set_time_alert_ns(f"rebalance-{at}", at, callback=self._on_alert)
            self.alert_pending = True
            return
        self._schedule(datetime.fromtimestamp(msg.ts / 1e9, NY))

    def _on_approved(self, msg):
        self.weights.update(msg.weights)
        self.halted = msg.halted  # follows the RiskAgent: halted until its breaker is cleared
        self.reduce_only = msg.reduce_only

    def _schedule_session_exit(self, after):
        from lab.agents.schedule import is_session_day

        sess = self.config.session
        day = after
        for _ in range(10):
            at = sess.close_at(day) - sess._m(sess.interval_min)
            if at > after and is_session_day(day, sess.weekdays_only):
                self.clock.set_time_alert_ns("session-exit", int(at.timestamp() * 1e9), callback=self._on_session_exit)
                return
            day += timedelta(days=1)

    def _on_session_exit(self, event):
        self.rebalance()  # independent of new_data or a successful historical-data request
        self._schedule_session_exit(datetime.fromtimestamp(event.ts_event / 1e9, NY))

    def _retry_liquidation(self):
        if self.config.live and self.liquidation_requested and not self.exit_retry_pending:
            self.exit_retry_pending = True
            self.clock.set_time_alert_ns(
                "exit-retry", self.clock.timestamp_ns() + 10_000_000, callback=self._on_exit_retry
            )

    def _on_exit_retry(self, _event):
        self.exit_retry_pending = False
        self.rebalance()

    def _schedule(self, after: datetime):
        nxt = next_at(after, self.config.rebalance_time, weekdays_only=self.config.weekdays_only)
        self.clock.set_time_alert_ns(
            f"rebalance-{nxt:%Y%m%d-%H%M%S}", int(nxt.timestamp() * 1e9), callback=self._on_alert
        )
        self.alert_pending = True

    def _on_alert(self, event):
        self.alert_pending = False
        if self.config.session is not None:
            if self.new_data:
                self.new_data = False
                self.rebalance()
            return  # the next bar schedules the next decision
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
        self._consume_adopt()
        self._consume_dispose()
        authority = getattr(self.config, "authority", None)
        if authority is not None:
            try:
                mandate = authority.read()
                if mandate["mode"] == "supervised" and any(r["approved"] for r in authority.requests()):
                    self.rebalance()
            except (OSError, ValueError, KeyError, sqlite3.Error):
                self._cancel_open_orders()
                self.blocked_reason = "operator authority unavailable or expired"
        if self.pending or self.disposed or self.terminal:
            self._recover_pending()  # safety net: settle journaled orders from the broker's latest state
        if self._kill():
            self._cancel_open_orders()
        # Exit controls must not depend on another market-data message arriving.
        kill = self._kill()
        closing = self.config.flat_at_close and self._session_phase() in ("closing", "closed")
        if (kill and kill.get("mode") == "flatten") or (closing and (any(self.held.values()) or self.pending)):
            self.rebalance()
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
                    "risk": self._risk_view(),
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
            if not math.isfinite(b) or not math.isfinite(mine) or abs(b - mine) > 1e-9:
                diffs.append(f"{t}: ledger {mine:g} vs broker {b:g}")
        return diffs

    def _adopt_broker_positions(self) -> bool:
        """Operator-requested reset: take the broker's positions as truth, at their cost basis."""
        if not self._recovery_account_ok():
            return False
        if self.orphans:
            self._hard_halt("reconciliation_mismatch", "restore orphaned instruments before adopting the book")
            return False
        self._recover_pending()
        if self.pending or any(
            self.cache.orders_open_count(instrument_id=iid) for iid in self.config.instruments.values()
        ):
            self._hard_halt(
                "reconciliation_mismatch",
                "cannot adopt while orders are open or unresolved; obtain terminal broker reports first. "
                "An account snapshot is not proof that an in-flight order cannot fill",
            )
            return False
        adopted = {}
        for t, iid in self.config.instruments.items():
            q = self._broker_position(t)
            if q:
                positions = self.cache.positions_open(instrument_id=iid)
                v = positions[0].avg_px_open if positions else 0.0
                avg = float(v() if callable(v) else v)
                adopted[t] = (q, avg)
        self.held = dict.fromkeys(self.config.instruments, 0.0)
        for t, (q, _) in adopted.items():
            self.held[t] = q
        if self.config.dedicated_account and math.isfinite(self._broker_cash()):
            # The lab owns the account: its cash is the broker's, not a reconstruction (G007-F1).
            self.cash = self._broker_cash()
            self.synced_cash = True
            basis = "broker cash"
        else:
            # An allocation: capital minus the cost of the adopted positions; the funding check then verifies
            # that the account can carry it.
            self.cash = self.config.capital - sum(q * avg for q, avg in adopted.values())
            basis = "capital minus cost basis"
        self.ledger_account = self._account_id() or self.ledger_account
        self._save()
        self._risk_event(
            "ledger_reset", f"adopted broker positions {adopted}; ledger cash now {self.cash:,.2f} ({basis})"
        )
        return True

    def _risk_view(self) -> dict:
        """The book's exposure at the engine's marks, for the operator (ChatGPT G014-F1): equity, gross, net,
        short, largest position and initial margin against the configured limits, and any breach. Computed
        here, from the reconciled ledger, never in the browser; marked unknown when a held mark is missing or
        stale, rather than shown from old prices."""
        unsafe = self._unsafe_held_marks()
        limits = getattr(self.config, "risk_limits", None)
        view = {
            "marks_current": not unsafe,
            "unsafe_marks": unsafe,
            "limits": (
                {
                    "max_weight": limits.max_weight,
                    "max_gross": limits.max_gross,
                    "max_short_gross": limits.max_short_gross,
                    "min_net": limits.min_net,
                    "max_net": limits.max_net,
                }
                if limits is not None
                else None
            ),
        }
        if unsafe:
            return view
        long_value, short_value = self._book()
        actual_in_cash = self.config.live and self.config.dedicated_account
        equity = self.cash + long_value - short_value - (0.0 if actual_in_cash else self.financing)
        if not math.isfinite(equity) or equity <= 0:
            return {
                **view,
                "equity": round(equity, 2) if math.isfinite(equity) else None,
                "breach": "no positive equity",
            }
        weights = {t: q * self.prices.get(t, 0.0) / equity for t, q in self.held.items() if q}
        margin = self.config.margin or MarginSettings()
        return {
            **view,
            "equity": round(equity, 2),
            "gross": sum(abs(w) for w in weights.values()),
            "net": sum(weights.values()),
            "short": -sum(min(w, 0) for w in weights.values()),
            "largest": max((abs(w) for w in weights.values()), default=0.0),
            "initial_margin": mg.initial_requirement(weights, margin),
            "breach": _held_breach(self.held, self.prices, equity, limits, margin),
        }

    def _book(self) -> tuple[float, float]:
        long_value = sum(q * self.prices.get(t, 0.0) for t, q in self.held.items() if q > 0)
        short_value = -sum(q * self.prices.get(t, 0.0) for t, q in self.held.items() if q < 0)
        return long_value, short_value

    def _accrue(self) -> None:
        """Charge borrow fees and debit interest, and credit interest on free cash and short proceeds, for the
        calendar days since the last accrual. `financing` is the net cost (negative when credits win)."""
        ts = self.price_ts
        verified_fee = None
        if self.borrow_ledger is not None and ts > self.accrued_to:
            verified_fee = self.borrow_ledger.close(datetime.fromtimestamp(ts / 1e9, NY).date(), self.held, self.prices)
        # Calendar days between session dates: the book is charged for each night it is held. Intraday
        # rebalances on the same date accrue nothing; a book that is flat at the close accrues nothing.
        days = (_ny_date(ts) - _ny_date(self.accrued_to)).days if self.accrued_to and ts > self.accrued_to else 0
        if days > 0:
            _, short_value = self._book()
            borrow, debit = mg.financing_cost(
                self.cash, short_value, days, self.config.margin, verified_borrow_fee=verified_fee
            )
            credit = mg.financing_credit(self.cash, short_value, days, self.config.margin)
            self.borrow_fees += borrow
            self.financing += borrow + debit - credit
        if ts and (not self.accrued_to or days > 0):
            self.accrued_to = ts
            self._save()  # persist every accrual: a restart must not re-accrue the span at a different book

    def snapshot(self) -> EquitySnapshot:
        self._accrue()
        long_value, short_value = self._book()
        # Dedicated account: the broker's actual charges enter the ledger's cash (booked as external cash), so
        # the estimate is diagnostic only and subtracting both would double-charge the book (ChatGPT, G009-F27).
        # Allocation mode: the lab's cash never sees the broker's charges, which land on the whole account, so
        # the estimate is the only financing the lab's equity carries (Claude: without it a short or levered book
        # looks richer every night than it is).
        actual_in_cash = self.config.live and self.config.dedicated_account
        equity = self.cash + long_value - short_value - (0.0 if actual_in_cash else self.financing)
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
        try:
            self._persist_state()
        except (OSError, ValueError, sqlite3.Error):
            # A memory-only fill/cap update must never authorize subsequent orders.
            # The disk may also be unable to persist a HALT, so latch in memory and stop.
            self.persistence_failed = True
            if self.config.live:
                self.shutdown_system("execution state could not be persisted; operator review required")
            raise

    def _persist_state(self):
        if self.config.state_path:
            p = Path(self.config.state_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.store.save(
                {
                    "version": 2,
                    "cash": self.cash,
                    "commissions": self.commissions,
                    "financing": self.financing,
                    "external_cash": self.external_cash,
                    "synced_cash": self.synced_cash,
                    "borrow_fees": self.borrow_fees,
                    "accrued_to": self.accrued_to,
                    "positions": {**self.orphans, **self._nonzero(self.held)},
                    "account": self.ledger_account,
                    "pending": self.pending,
                    # A restart within a session must not reset the day's order and turnover caps.
                    "daily_caps": {
                        "day": self.cap_day.isoformat() if self.cap_day else None,
                        "orders": self.cap_orders,
                        "opened": self.cap_opened,
                    },
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

    def _unsafe_held_marks(self) -> list[str]:
        """What makes the book unmarkable (live, and historical-borrow research): nothing may be sized then.

        Every held stock needs a positive, finite price from the same session as the newest price (G004-F5,
        ChatGPT), and that newest price must itself be current against the market clock, neither older than
        the bar-age limit nor from the future (G007-F2): two equally stale prices must not "agree"."""
        if not self.config.live and self.config.borrow_book is None:
            return []
        held = [t for t in self.held if self.held[t] or self.weights.get(t, 0)]
        if not self.price_ts:
            return ["<no prices yet>"] if held else []
        if self.config.live:
            stale = bar_freshness(
                self.price_ts, market_now(self.clock, self.config.market_offset_days), self.config.max_bar_age_days
            )
            if stale:
                return [f"<newest price: {stale}>"]
        session = datetime.fromtimestamp(self.price_ts / 1e9, NY).date()
        bad = []
        for ticker in held:
            mark, ts = self.prices.get(ticker), self.price_stamps.get(ticker)
            if (
                mark is None
                or not math.isfinite(mark)
                or mark <= 0
                or ts is None
                or datetime.fromtimestamp(ts / 1e9, NY).date() != session
                or (self.config.session is not None and ts != self.price_ts)
            ):
                bad.append(ticker)
        return bad

    def rebalance(self):
        if self.borrow_error:
            return  # backtest.run refuses to return metrics for this invalid run
        self._consume_adopt()

        # Positions first: they need no prices, and nothing may trade on a book that disagrees with the broker.
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

        if self.config.session is not None:
            self._cancel_stale_orders()
        unsafe = self._unsafe_held_marks()
        snap = None
        if not unsafe:
            try:
                snap = self.snapshot()
            except BorrowUnavailable as exc:
                self.borrow_error = str(exc)
                self._risk_event("borrow_history_invalid", self.borrow_error)
                return
            self.publish_message(T_EQUITY, snap)  # RiskAgent may halt here; its reply arrives synchronously
        elif self.borrow_ledger is not None:
            self.borrow_error = f"missing/stale marks for held positions: {', '.join(sorted(unsafe))}"

        halt = self._halt()
        if halt_kinds(halt) - _REDUCE_ONLY_HALTS:
            self.blocked_reason = f"hard halt: {halt['kind']}"
            self._risk_event("hard_halt", f"{halt['kind']} since {halt.get('at')}: no orders (lab recover clears it)")
            return
        kill = self._kill()
        flatten = bool(kill and kill.get("mode") == "flatten")
        if kill and not flatten:
            self._cancel_open_orders()
            self._risk_event("kill_switch", f"halted by kill switch ({kill.get('reason', '')}); no orders sent")
            return

        equity_ok = snap is not None and math.isfinite(snap.equity) and snap.equity > 0
        if snap is not None and not equity_ok and any(self.held.values()):
            detail = f"equity {snap.equity:,.2f}: nothing can be sized; every position is exited by quantity"
            self._risk_event("liquidation", detail)
            if self.config.live:
                self._hard_halt("margin_call", detail)
        breaker = "circuit_breaker" in halt_kinds(halt)
        phase = self._session_phase()
        closing = self.config.flat_at_close and phase in ("closing", "closed")
        liquidate = flatten or self.halted or breaker or closing or (snap is not None and not equity_ok)
        if unsafe and not liquidate:
            self.blocked_reason = f"missing/stale marks for held positions: {', '.join(sorted(unsafe))}"
            self._risk_event("stale_data", self.blocked_reason + "; no orders until all held marks are current")
            return
        self.blocked_reason = ""
        if liquidate:
            if closing and any(self.held.values()) and self.flattened_day != self._now().date():
                self.flattened_day = self._now().date()
                self._risk_event("flat_at_close", "session exit: cancel entries and close reconciled quantities")
            self._liquidate()
            return
        if phase == "closed":
            return  # no quick-speed entries outside the regular session, even when overnight holds are allowed
        self.liquidation_requested = False
        if self.config.live and not self._check_funding(snap):
            return
        if self.config.live:
            snap = self.snapshot()  # include any dedicated-account cash adjustment before sizing
            self.publish_message(T_EQUITY, snap)
            if self.halted or snap.equity <= 0:
                self._liquidate()
                return

        desired = self.weights
        authority = getattr(self.config, "authority", None)
        if authority is not None:
            try:
                mandate = authority.read()
                self.authority_nonce = mandate["nonce"]
                if mandate["mode"] == "manual":
                    desired = mandate["targets"]
            except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                self._cancel_open_orders()
                self.blocked_reason = f"authority blocked: {exc}"
                self._risk_event("authority_blocked", self.blocked_reason)
                return
        reduce_only = self.reduce_only or bool(halt_kinds(halt) & {"margin_call", "cash_mismatch"})
        # A held book past a hard limit at the decision marks (drift from prices, interest or fees): the
        # reductions that restore it go out whatever their size (G013-F1, Gemini audit F-R13). The limits bind
        # decisions and submissions; marked holdings can still cross them between decisions, so every breach
        # is reported with the book's exposure, and one that no order can correct is reported again.
        margin = getattr(self.config, "margin", None)
        breach = _held_breach(self.held, self.prices, snap.equity, getattr(self.config, "risk_limits", None), margin)
        if breach:
            self._risk_event("cap_breach", f"{breach}; {_exposure_note(self.held, self.prices, snap.equity, margin)}")
        corrective = 0
        orders = []
        borrow_blocked = False
        for ticker, iid in self.config.instruments.items():
            price = self.prices.get(ticker)
            if not price:
                continue
            current = self.held.get(ticker, 0.0)
            # A breaker halt, the kill switch's flatten or a halted RiskAgent exit everything.
            exit_all = self.halted or flatten or (halt is not None and not reduce_only)
            w = 0.0 if exit_all else desired.get(ticker, 0.0)
            target = math.trunc(w * snap.equity * (1 - self.config.cash_buffer) / price)  # signed, toward zero
            if current * target < 0:
                target = 0  # settle the exit before opening the opposite side on a later decision
            if reduce_only:
                # Margin call: exposure may only shrink, never flip or grow.
                target = 0 if target * current <= 0 else math.copysign(min(abs(target), abs(current)), current)
            if self.config.borrow_book is not None:
                target, reason = self.config.borrow_book.restrict_target(
                    ticker, current, target, datetime.fromtimestamp(self.price_ts / 1e9, NY).date()
                )
                if reason:
                    borrow_blocked = True
                    self._risk_event("borrow_unavailable", reason + "; new short exposure blocked")
            cap = _liquidity_cap(
                getattr(self, "volumes", {}).get(ticker), getattr(self.config, "max_participation", None)
            )
            if cap is not None and _opening(current, target) > cap:
                # Gemini audit F-R06: new exposure trades at most max_participation of the average bar volume;
                # the rest follows on later decisions. Reductions are never held back by liquidity.
                target = math.trunc(_with_opening(current, target, cap))
                self._risk_event("liquidity_cap", f"{ticker}: new exposure capped at {cap:,.0f} shares this bar")
            diff = target - current
            if diff == 0:
                continue
            value = abs(diff) * price
            restores = bool(breach) and current != 0 and target * current >= 0 and abs(target) < abs(current)
            if target != 0 and value < self.config.min_trade_value and not restores:
                continue
            if (
                target != 0
                and current != 0
                and target * current > 0
                and value < self.config.rebalance_band * snap.equity
                and not restores
            ):
                continue  # small drift: not worth the commission
            if self.cache.orders_open_count(instrument_id=iid) > 0:
                continue
            if any(p["ticker"] == ticker for p in self.pending.values()):
                # An earlier order for this stock is not settled yet: never risk a duplicate.
                self._risk_event("order_pending", f"{ticker}: previous order still unsettled; no new order today")
                continue
            orders.append((_opening(current, target), target, current, iid, ticker, price))
            corrective += restores

        if breach and not corrective:
            self._risk_event(
                "cap_breach_unresolved",
                f"{breach}: no corrective order could be sent this decision (pending or open orders, missing "
                "marks or no reducing target); additions stay blocked",
            )
        orders.sort(key=lambda o: o[0] > 0)
        # A reduction is not settled until its broker report arrives. Gross-only
        # headroom cannot enforce short/net/margin limits under partial fills (ChatGPT, G009).
        # With the risk limits known, every addition is checked against the worst case over every subset of
        # this batch's fills, reductions included (lab.reservations): one that passes is safe beside an
        # unfilled reduction, so it need not wait (Claude: a blanket wait starves a book trimmed every day).
        # Without them (component tests), nothing is added until reductions settle.
        reductions = any(o[0] == 0 for o in orders) and getattr(self.config, "risk_limits", None) is None
        unsettled = bool(self.pending) or any(
            self.cache.orders_open_count(instrument_id=iid) for iid in self.config.instruments.values()
        )
        day = _ny_date(self.price_ts) if self.price_ts else None
        if day != self.cap_day:
            self.cap_day, self.cap_orders, self.cap_opened = day, 0, 0.0
        reserved = {}
        for opening, target, current, iid, ticker, price in orders:
            if opening > 0 and unsettled:
                self._risk_event("order_pending", "new exposure deferred until existing orders settle")
                continue
            if opening > 0 and reductions:
                self._risk_event("order_pending", f"{ticker}: new exposure deferred until reductions settle")
                continue
            if borrow_blocked and opening > 0:
                # Do not open the long hedge of an unavailable short (or any other
                # new exposure from the now-incomplete approved portfolio).
                target = _with_opening(current, target, 0)
                opening = 0
            if (
                opening > 0
                and getattr(self.config, "notification_health", None) is not None
                and not self.config.notification_health()["healthy"]
            ):
                self.blocked_reason = "notification delivery unavailable; no new exposure"
                continue
            if opening > 0 and opening * price > self.config.max_order_value:
                cap = math.floor(self.config.max_order_value / price)
                target = _with_opening(current, target, cap)
                opening = cap
                self._risk_event(
                    "order_capped", f"{ticker}: new exposure capped at {self.config.max_order_value:,.0f} USD"
                )
            if opening > 0 and self.cap_orders >= self.config.max_orders_per_day:
                self._risk_event("daily_cap", f"max {self.config.max_orders_per_day} orders/day reached; rest deferred")
                break
            if opening > 0 and (self.cap_opened + opening * price) / snap.equity > self.config.max_daily_turnover:
                self._risk_event(
                    "daily_cap",
                    f"daily cap on new exposure {self.config.max_daily_turnover:.0%} of equity reached; rest deferred",
                )
                break
            if opening > 0 and getattr(self.config, "risk_limits", None) is not None:
                from lab.reservations import check_openings

                candidate = {**reserved, ticker: target}
                if getattr(self.config, "reservation_costs", None) is not None:
                    reason = self._reserve(candidate, snap.equity)
                else:
                    reason = check_openings(
                        self.held, candidate, self.prices, snap.equity, self.config.risk_limits, self.config.margin
                    )
                if reason and getattr(self.config, "reservation_costs", None) is not None:
                    # Fit a smaller addition inside the stressed budget. Each tested target is
                    # checked with the entire current book and all earlier outstanding orders.
                    lo, hi = 0, math.floor(opening)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        trial = _with_opening(current, target, mid)
                        refusal = self._reserve({**reserved, ticker: trial}, snap.equity)
                        if refusal:
                            hi = mid - 1
                        else:
                            lo = mid
                    if lo and lo * price >= self.config.min_trade_value:
                        target = _with_opening(current, target, lo)
                        opening = lo
                        reason = self._reserve({**reserved, ticker: target}, snap.equity)
                if reason:
                    self._risk_event("reservation_blocked", f"{ticker}: {reason}")
                    continue
            diff = target - current
            if diff != 0:
                # Include caps in the same write-ahead snapshot as the intent, before submission.
                # A crash after submission must not restore yesterday's remaining allowance.
                previous_caps = self.cap_orders, self.cap_opened
                self.cap_orders += 1
                self.cap_opened += opening * price
                if self._submit(iid, ticker, diff, adds=opening > 0):
                    reserved[ticker] = target
                    self._save()
                else:
                    self.cap_orders, self.cap_opened = previous_caps

    def _reserve(self, destinations: dict, equity: float) -> str | None:
        """Exposure caps at decision marks; cash and margin under the adverse gap (lab.reservations)."""
        from lab.reservations import split_openings

        return split_openings(
            self.held,
            destinations,
            self.prices,
            equity,
            self.config.risk_limits,
            self.config.margin,
            self.config.reservation_costs,
            self.config.entry_slippage,
            self.config.reservation_gap,
        )

    def _now(self) -> datetime:
        return market_now(self.clock, self.config.market_offset_days)

    def _session_phase(self) -> str | None:
        """Quick speeds: where the decision falls in the regular session: "open", "closing" when at most one
        interval remains (orders still fill on the session's last bar), or "closed". None at the daily speed."""
        return None if self.config.session is None else self.config.session.phase(self._now())

    def _cancel_stale_orders(self):
        """Quick speeds, live: a limit order still working one bar after it was sent is cancelled; the next
        decision re-prices it from fresh data."""
        sess = self.config.session
        limit = sess.interval_min * sess.seconds_per_minute * 1e9
        now = self.clock.timestamp_ns()
        for iid in self.config.instruments.values():
            for order in self.cache.orders_open(instrument_id=iid):
                if now - order.ts_init >= limit:
                    self.cancel_order(order.client_order_id)

    def _consume_adopt(self) -> bool:
        """Apply an operator's `lab recover --adopt-broker` request (the broker's book is the truth). It runs
        only after pending orders have terminal broker reports; a snapshot cannot dispose an unknown order."""
        if self.config.adopt_path and Path(self.config.adopt_path).exists():
            accepted = self._adopt_broker_positions()
            Path(self.config.adopt_path).unlink()
            return accepted
        return False

    def _liquidate(self):
        """Exit every held position by quantity: no equity, price, cap or band can block an exit."""
        self.liquidation_requested = True
        # Cancel any working entry or resize before covering the remaining position.
        # Wait for the terminal broker event; a cancel request is not cancellation.
        for coid, info in list(self.pending.items()):
            order = self.cache.order(ClientOrderId(coid))
            if order is not None and not order.is_closed and info.get("adds", True):
                self.cancel_order(order.client_order_id)
        for ticker, iid in self.config.instruments.items():
            current = self.held.get(ticker, 0.0)
            if not current:
                continue
            if self.cache.orders_open_count(instrument_id=iid) > 0 or any(
                p["ticker"] == ticker for p in self.pending.values()
            ):
                # An exit (or an earlier order) is already working: a second one could reverse the position.
                continue
            self._submit(iid, ticker, -current)

    def _broker_cash(self) -> float:
        """Cash the broker reports. Live: the account's reported balance. Backtest: the simulated margin
        account's cash identity (account_cash)."""
        if not self.config.live:
            return self.account_cash()
        acct = self.cache.account_for_venue(self.config.venue)
        bal = acct.balance_total() if acct is not None else None
        return float(bal.as_double()) if bal is not None else float("nan")

    def _check_funding(self, snap) -> bool:
        """The broker account must be able to carry the lab's book (G007-F1). Returns False to stop trading.

        allocation (default): the lab trades `capital` of a possibly larger paper account, so cash need not
            match; the broker's net liquidation value must cover the lab's equity.
        dedicated: the lab owns the account. Small unexplained cash changes (dividends, interest) are booked
            into the ledger with an audit event; anything larger is a hard halt.
        """
        c = self.config
        cash = self._broker_cash()
        if not math.isfinite(cash):
            self._hard_halt("cash_mismatch", "the broker reported no usable cash balance")
            return False
        marks = {t: self.prices.get(t, 0.0) for t in c.instruments}
        broker_nlv = cash + sum(self._broker_position(t) * marks[t] for t in c.instruments)
        if not math.isfinite(broker_nlv) or not math.isfinite(snap.equity):
            self._hard_halt("cash_mismatch", "nonfinite broker valuation or ledger equity")
            return False
        tol = c.cash_tolerance + 0.001 * abs(snap.equity)
        if c.dedicated_account:
            unexplained = cash - self.cash
            if abs(unexplained) <= c.cash_tolerance:  # the lab owns the cash: anything beyond cents is explained
                if not self.synced_cash:
                    self.synced_cash = True
                    self._save()
                return True
            if abs(unexplained) <= c.max_external_cash * abs(snap.equity) and self.synced_cash:
                self.cash += unexplained
                self.external_cash += unexplained
                self._save()
                self._risk_event(
                    "external_cash",
                    f"broker cash moved {unexplained:+,.2f} USD beyond the lab's trades (dividend, interest, fee or "
                    "transfer); booked into the ledger",
                )
                return True
            self._hard_halt(
                "cash_mismatch",
                f"dedicated account: broker cash {cash:,.2f} vs ledger {self.cash:,.2f} ({unexplained:+,.2f})",
            )
            return False
        if broker_nlv < snap.equity - tol:
            self._hard_halt(
                "cash_mismatch",
                f"the account cannot fund the lab's book: broker net liquidation {broker_nlv:,.2f} USD < "
                f"ledger equity {snap.equity:,.2f} USD",
            )
            return False
        return True

    def _submit(self, iid, ticker: str, diff: float, *, adds: bool = False) -> bool:
        """adds: the order opens new exposure (live quick speeds send it as a collared limit order)."""
        if self.persistence_failed:
            return False
        if self.config.live and not self._paper_guard():
            return False
        inst = self.cache.instrument(iid)
        if inst is None:
            self._risk_event("blocked", f"{ticker}: instrument {iid} not loaded; order skipped")
            return False
        side = OrderSide.BUY if diff > 0 else OrderSide.SELL
        qty = inst.make_qty(abs(diff))
        if self.config.session is None:
            tif = TimeInForce.AT_THE_OPEN if self.config.live else TimeInForce.GTC
            order = self.order_factory.market(iid, side, qty, time_in_force=tif)
        elif adds and self.prices.get(ticker):
            # A marketable limit: delayed data can be stale, so new exposure never pays more than the collar.
            c = self.config.limit_collar
            px = self.prices[ticker] * (1 + c if side == OrderSide.BUY else 1 - c)
            order = self.order_factory.limit(
                iid, side, qty, inst.make_price(round(px, inst.price_precision)), time_in_force=TimeInForce.DAY
            )
        else:
            order = self.order_factory.market(iid, side, qty, time_in_force=TimeInForce.DAY)  # exits must go
        coid = str(order.client_order_id)
        if coid in self.pending or coid in self.disposed or coid in self.terminal:
            # Never reuse an identifier the broker may already know: it would be refused, or worse, matched.
            self._risk_event("blocked", f"{ticker}: client order ID {coid} already journaled; order not sent")
            return False
        authority = getattr(self.config, "authority", None)
        if authority is not None:
            order_request = {
                "ticker": ticker,
                "side": "BUY" if diff > 0 else "SELL",
                "qty": abs(diff),
                "decision_price": self.prices.get(ticker),
                "decision_ts": self.price_ts,
                "book": {"cash": self.cash, "positions": self.held},
            }
            emergency = (
                self.liquidation_requested
                and not adds
                and abs(diff) <= abs(self.held.get(ticker, 0))
                and diff * self.held.get(ticker, 0) < 0
            )
            try:
                permitted = authority.authorize(order_request, emergency=emergency, expected_nonce=self.authority_nonce)
            except (OSError, ValueError, KeyError, sqlite3.Error):
                permitted = False
            if not permitted:
                self._risk_event("approval_required", f"{ticker}: order awaits current operator authority/approval")
                return False
        self.pending[coid] = {
            "ticker": ticker,
            "side": "BUY" if diff > 0 else "SELL",
            "qty": abs(diff),
            "ts": self.clock.timestamp_ns(),
            "adds": adds,
            "decision_price": self.prices.get(ticker),
            "decision_ts": self.price_ts,
            "filled": 0.0,
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

    def _recovery_account_ok(self) -> bool:
        """Do not mutate a ledger from reports for a missing/different account."""
        if not self.config.live:
            return True
        account = self._account_id()
        if account is None or not self._paper_guard():
            return False
        if self.ledger_account not in (None, account):
            self._hard_halt("reconciliation_mismatch", "broker reports belong to another ledger account")
            return False
        return True

    def _apply_fill(self, ev, *, recovered: bool = False) -> bool:
        """Book one broker fill into the ledger, once per trade ID. Returns False for a repeat."""
        trade = str(ev.trade_id)
        if trade in self.applied:
            return False
        qty, px = float(ev.last_qty), float(ev.last_px)
        fee = float(ev.commission.as_double()) if ev.commission is not None else 0.0
        buy = ev.order_side == OrderSide.BUY
        ticker = str(ev.instrument_id.symbol)
        info = self.pending.get(str(ev.client_order_id), {})
        if (
            not all(math.isfinite(v) for v in (qty, px, fee))
            or qty <= 0
            or px <= 0
            or (info and (info["ticker"] != ticker or info["side"] != ("BUY" if buy else "SELL")))
        ):
            self._hard_halt("reconciliation_mismatch", f"invalid or mismatched fill for order {ev.client_order_id}")
            return False
        if info:
            info["filled"] = info.get("filled", 0.0) + qty
        # Cash settles in cents, half away from zero, as the broker's (and the simulator's) account does.
        notional = math.floor(qty * px * 100 + 0.5) / 100
        self.cash += -notional - fee if buy else notional - fee
        self.commissions += fee
        self.held[ticker] = self.held.get(ticker, 0.0) + (qty if buy else -qty)
        self.applied[trade] = [time.time_ns(), str(ev.client_order_id)]
        self._prune_applied()
        self._save()
        self.publish_message(
            T_FILL,
            FillReport(
                ev.ts_event,
                ticker,
                "BUY" if buy else "SELL",
                qty,
                px,
                fee,
                str(ev.client_order_id),
                info.get("decision_price"),
                info.get("decision_ts"),
            ),
        )
        if recovered:
            self._risk_event(
                "fill_recovered",
                f"{ticker} {'BUY' if buy else 'SELL'} {qty:g} @ {px:.2f} (trade {trade}) filled while the node was down; "
                "booked from the broker's report",
            )
        return True

    def _close_pending(self, client_order_id) -> None:
        if not self._recovery_account_ok():
            return
        coid = str(client_order_id)
        if coid in self.pending:
            # A terminal notification may arrive before its fill callbacks. Recover cached
            # executions first and keep the intent for reports that arrive still later.
            order = self.cache.order(ClientOrderId(coid))
            if order is not None:
                for event in order.events():
                    if type(event).__name__ == "OrderFilled":
                        self._apply_fill(event, recovered=True)
            self.terminal[coid] = self.pending.pop(coid)
            self._save()

    def _recover_pending(self) -> None:
        """After a restart: settle every journaled order from what the broker reported at reconciliation."""
        if not self._recovery_account_ok():
            return
        # Inspect broker-reported IDs, not every historical order on every heartbeat.
        if getattr(self, "store", None):
            reported = (str(o.client_order_id) for o in self.cache.orders())
            archived = ((coid, self.terminal.get(coid) or self.disposed.get(coid)) for coid in reported)
        else:
            archived = {**self.disposed, **self.terminal}.items()
        for coid, info in archived:
            if info is None:
                continue
            if coid in self.pending:
                continue
            order = self.cache.order(ClientOrderId(coid))
            if order is None:
                continue
            unseen = any(
                type(e).__name__ == "OrderFilled" and str(e.trade_id) not in self.applied for e in order.events()
            )
            if (coid in self.terminal or info.get("reactivated")) and order.is_closed and not unseen:
                continue
            self.pending[coid] = dict(info)
            info["reactivated"] = True
            (self.terminal if coid in self.terminal else self.disposed)[coid] = info
            self._hard_halt(
                "reconciliation_mismatch",
                f"{info['ticker']}: archived order {coid} reappeared in broker reports; review its evidence",
            )
            self._save()
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
                    "possible in-flight order. Preserve intent and require audited investigation. "
                    "Elapsed time is not evidence of whether it filled."
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

    def _consume_dispose(self) -> None:
        """Apply an operator's `lab recover --dispose-order COID --evidence ...`: an unconfirmed journaled intent
        that the broker's own records show never executed (its order and trade history, read by the operator).
        Refused while the broker reports the order, or while its stock's position disagrees with the ledger
        (a fill could be missing). The intent moves to `disposed` with the evidence; it is never erased, so a
        fill that still arrives is booked to the lab (and halts for review)."""
        path = Path(self.config.dispose_path) if self.config.dispose_path else None
        if path is None or not path.exists():
            return
        if self.config.live and not self._paper_guard():
            return
        if self.ledger_account not in (None, self._account_id()):
            self._risk_event("dispose_refused", "broker account differs from the ledger; nothing disposed")
            return
        try:
            req = read_json(path)
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
            orders, evidence = req["orders"], req["evidence"]
            if (
                not isinstance(orders, list)
                or not orders
                or any(not isinstance(coid, str) or not coid.strip() for coid in orders)
                or not isinstance(evidence, str)
                or not evidence.strip()
            ):
                raise ValueError("nonempty order IDs and textual broker evidence required")
            evidence = evidence.strip()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._risk_event("dispose_refused", f"unreadable disposal request ({exc}); nothing disposed")
            durable_unlink(path)
            return
        diffs = self.reconcile()
        for coid in orders:
            info = self.pending.get(coid)
            reason = None
            if info is None:
                reason = "not an unconfirmed journaled intent"
            elif not evidence:
                reason = "no broker evidence given"
            elif info.get("filled", 0) or (
                self.applied.order_has_fill(coid)
                if getattr(self, "store", None)
                else any(order_id == coid for _, order_id in self.applied.values())
            ):
                reason = "the ledger already records a fill; settle it from broker reports instead"
            elif self.cache.order(ClientOrderId(coid)) is not None:
                reason = "the broker reports this order: settle it from its reports instead"
            elif any(d.startswith(f"{info['ticker']}:") for d in diffs):
                reason = f"{info['ticker']}'s position disagrees with the ledger: a fill may be missing"
            if reason:
                self._risk_event("dispose_refused", f"{coid}: {reason}; the intent is kept")
                continue
            self.disposed[coid] = {
                **info,
                "evidence": evidence,
                "reason": str(req.get("reason", "")),
                "disposed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            del self.pending[coid]
            self._risk_event(
                "order_disposed",
                f"{coid} ({info['ticker']} {info['side']} {info['qty']:g}) disposed on evidence: {evidence}",
            )
        self._save()
        durable_unlink(path)

    def _prune_applied(self) -> None:
        """Prune terminal attributed IDs only; archival capacity and broker horizon remain release gates."""
        if getattr(self, "store", None):
            return  # Indexed history is permanent; capacity exhaustion stops execution.
        cutoff = time.time_ns() - _KEEP_TRADE_DAYS * 86_400 * 10**9
        stale = [
            t
            for t, (ts, coid) in self.applied.items()
            if ts < cutoff
            and coid is not None
            and coid not in self.pending
            and coid not in self.disposed
            and coid not in self.terminal
        ]
        for t in stale:
            del self.applied[t]

    def on_order_filled(self, ev):
        if not self._recovery_account_ok():
            return
        coid = str(ev.client_order_id)
        if str(ev.trade_id) in self.applied:
            return
        if self.config.live and coid not in self.pending and (coid in self.disposed or coid in self.terminal):
            # Keep attribution permanently and the active intent until a terminal report.
            info = self.terminal.get(coid) or self.disposed[coid]
            if coid not in self.pending:
                self.pending[coid] = dict(info)
            info["reactivated"] = True
            (self.terminal if coid in self.terminal else self.disposed)[coid] = info
            self._hard_halt(
                "reconciliation_mismatch",
                f"{info['ticker']}: order {coid} filled after terminal/disposal evidence "
                f"({info.get('evidence', 'broker terminal report')}); review before trading",
            )
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
        self._retry_liquidation()

    def on_order_canceled(self, ev):
        self._close_pending(ev.client_order_id)
        self._retry_liquidation()

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
