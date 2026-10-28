"""
Pre-trade risk for order-level (fast) strategies: one RiskManager per trader, and every strategy is a
RiskManagedStrategy, so every order it sends passes the same portfolio-wide checks first.

Two layers, each order must pass both:

1. RiskManager (this module), across all strategies and instruments of the trader:
   - kill switch: the lab kill file (`lab kill`, lab.safety), re-read every second. "halt" stops all orders and
     cancels open ones; "flatten" also exits every position. Any unreadable kill file halts.
   - daily loss cap: equity (account balances + unrealized PnL at the mark, else the mid) against the equity at the
     first look of the UTC day. Once the loss reaches the cap, open orders are cancelled, positions are exited and
     only orders that reduce a position pass until the next UTC day. The day's starting equity and the breach
     persist in a state file, so a restart neither forgets the loss nor clears the stop.
   - position limits: the notional of each instrument's worst-case position (current position plus every live
     order on the same side, plus this order) and the sum of those over all instruments (gross).
   - a cap on live orders.
   An order that only reduces a position (reduce-only, or opposite to the position and no larger than what is left
   of it after other live reducing orders) always passes, except during a kill-switch halt, so a stop can always
   be exited. Anything the checks cannot evaluate (no account, no price, a money limit in another currency, an
   unreadable state file, an exception) denies the order: fail closed.

2. NautilusTrader's RiskEngine (`engine_config`), in Rust: order submit/modify rate limits, a maximum notional
   per order, price/quantity precision, free balance and reduce-only checks.

Trading states follow NautilusTrader's names: ACTIVE, REDUCING (only reducing orders), HALTED (no orders). The
native engine's own trading state cannot be set from Python in 2.0.0rc7, so the RiskManager keeps its own.

Not covered: a strategy that calls `Strategy.submit_order(self, ...)` directly, or that is added to the node
without `add_strategy` below, bypasses layer 1 (layer 2 still applies). Positions not owned by a
RiskManagedStrategy (external or manual) count toward the limits but are not exited by the risk manager. Money
limits are in one currency (`RiskLimits.currency`); instruments valued in another are denied, not converted.
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from nautilus_trader.model import Currency, InstrumentId, OrderSide, OrderStatus, Price, PriceType, TimeInForce, Venue
from nautilus_trader.trading import Strategy

from lab.persistence import append_json, atomic_json, read_json
from lab.safety import read_kill_switch

CHECK_INTERVAL = timedelta(seconds=1)
CHECK_INTERVAL_NS = 1_000_000_000
_RATE = re.compile(r"\d+/\d{2}:\d{2}:\d{2}")
_CLOSED = {OrderStatus.DENIED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.FILLED}


class TradingState(StrEnum):
    ACTIVE = "ACTIVE"
    REDUCING = "REDUCING"
    HALTED = "HALTED"


@dataclass(frozen=True)
class RiskLimits:
    """Money limits in `currency` (the account and quote currency, e.g. USDT). Rates are NautilusTrader's
    "<count>/HH:MM:SS"."""

    currency: str
    max_daily_loss: float
    max_position_notional: float  # per instrument, worst case
    max_gross_notional: float  # all instruments, worst case
    max_order_notional: float  # per order, enforced by NautilusTrader's RiskEngine
    max_live_orders: int = 20
    max_order_submit_rate: str = "100/00:00:01"
    max_order_modify_rate: str = "100/00:00:01"

    def __post_init__(self):
        for name in ("max_daily_loss", "max_position_notional", "max_gross_notional", "max_order_notional"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number, got {value!r}")
        if self.max_position_notional > self.max_gross_notional:
            raise ValueError("max_position_notional cannot exceed max_gross_notional")
        if (
            isinstance(self.max_live_orders, bool)
            or not isinstance(self.max_live_orders, int)
            or self.max_live_orders < 1
        ):
            raise ValueError(f"max_live_orders must be a positive integer, got {self.max_live_orders!r}")
        for name in ("max_order_submit_rate", "max_order_modify_rate"):
            if not _RATE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must look like 100/00:00:01, got {getattr(self, name)!r}")
        if not self.currency:
            raise ValueError("currency is required")


def engine_config(limits: RiskLimits, instrument_ids, *, live: bool = False):
    """NautilusTrader's own RiskEngine settings for these limits: the second, native layer."""
    if live:
        from nautilus_trader.live import LiveRiskEngineConfig as Config
    else:
        from nautilus_trader.risk import RiskEngineConfig as Config
    return Config(
        bypass=False,
        max_order_submit_rate=limits.max_order_submit_rate,
        max_order_modify_rate=limits.max_order_modify_rate,
        max_notional_per_order={str(i): str(limits.max_order_notional) for i in instrument_ids},
    )


# ----------------------------------------------------------------------------- the decision, on plain numbers


@dataclass
class Exposure:
    """One instrument, across every strategy of the trader. Quantities in base units, `unit_notional` is the
    notional of one unit at the reference price in `currency` (None: no price)."""

    net: float = 0.0
    live_buy: float = 0.0  # leaves quantity of live orders that can add to the position
    live_sell: float = 0.0
    reducing_buy: float = 0.0  # leaves quantity of live reduce-only orders
    reducing_sell: float = 0.0
    unit_notional: float | None = None
    currency: str | None = None

    def worst_case(self, buy: float = 0.0, sell: float = 0.0) -> float:
        return max(abs(self.net + self.live_buy + buy), abs(self.net - self.live_sell - sell))


@dataclass(frozen=True)
class Intent:
    instrument: str
    side: str  # "BUY" or "SELL"
    quantity: float
    reduce_only: bool = False
    limit_unit_notional: float | None = None  # at the order's own price, when the market has none
    limit_currency: str | None = None


@dataclass
class _Day:
    day: str
    start_equity: float
    breach: dict | None = None


@dataclass
class RiskManager:
    """Shared by every strategy of one trader. `venues`: whose account equity the daily loss cap watches."""

    limits: RiskLimits
    venues: list[str]
    kill_file: str | None = None
    state_path: str | None = None
    audit_path: str | None = None
    state: TradingState = TradingState.ACTIVE
    reason: str = ""
    denials: dict = field(default_factory=dict)  # denial code -> count
    passed: dict = field(default_factory=dict)  # client order ids this gate passed that may still be live
    _day: _Day | None = None
    _last_denial: tuple | None = None
    _today: str | None = None
    _state_error: str | None = None
    _kill: dict | None = None
    _kill_read_ns: int | None = None

    def __post_init__(self):
        if not self.venues:
            raise ValueError("the risk manager needs the venues whose equity it watches")
        if self.state_path and Path(self.state_path).exists():
            try:
                raw = read_json(self.state_path)
                if not isinstance(raw, dict):
                    raise ValueError("not a JSON object")
                self._day = _Day(str(raw["day"]), float(raw["start_equity"]), raw.get("breach"))
                if not math.isfinite(self._day.start_equity) or not isinstance(self._day.breach, dict | None):
                    raise ValueError("invalid values")
            except (OSError, ValueError, KeyError, TypeError) as e:
                self._day = None
                self._state_error = f"unreadable risk state {self.state_path}: {e}"

    # -- state

    def update(self, now_ns: int, equity: float | None) -> bool:
        """Re-evaluate the kill switch and the daily loss. Returns True when the state changed."""
        day = datetime.fromtimestamp(now_ns / 1e9, UTC).date().isoformat()
        self._today = day
        if equity is not None and (self._day is None or self._day.day != day) and self._state_error is None:
            self._day = _Day(day, equity)
            self._save()
            self._audit(now_ns, "day_start", {"day": day, "start_equity": equity})
        if self._day is not None and self._day.day == day and self._day.breach is None and equity is not None:
            loss = self._day.start_equity - equity
            if loss >= self.limits.max_daily_loss:
                self._day.breach = {"loss": loss, "equity": equity, "at_ns": now_ns}
                self._save()
        state, reason = self._target_state(now_ns, day)
        if (state, reason) == (self.state, self.reason):
            return False
        self._audit(now_ns, "state", {"from": self.state.value, "to": state.value, "reason": reason})
        self.state, self.reason = state, reason
        return True

    def _target_state(self, now_ns: int, day: str) -> tuple[TradingState, str]:
        if self._state_error:
            return TradingState.HALTED, self._state_error
        if self.kill_file and (self._kill_read_ns is None or now_ns - self._kill_read_ns >= CHECK_INTERVAL_NS):
            self._kill, self._kill_read_ns = read_kill_switch(self.kill_file), now_ns  # at most once a second
        kill = self._kill
        if kill and kill["mode"] == "halt":
            return TradingState.HALTED, f"kill switch (halt): {kill.get('reason', '')}"
        if kill:
            return TradingState.REDUCING, f"kill switch (flatten): {kill.get('reason', '')}"
        if self._day is not None and self._day.day == day and self._day.breach:
            b = self._day.breach
            return TradingState.REDUCING, (
                f"daily loss {b['loss']:.2f} {self.limits.currency} reached the cap of "
                f"{self.limits.max_daily_loss:.2f} on {day}; reducing only until the next UTC day"
            )
        return TradingState.ACTIVE, ""

    def _save(self):
        if self.state_path and self._day is not None:
            try:
                atomic_json(self.state_path, asdict(self._day))
            except OSError as e:  # a stop that cannot be persisted would not survive a restart: halt instead
                self._state_error = f"cannot write risk state {self.state_path}: {e}"

    def _audit(self, now_ns: int, kind: str, detail: dict):
        if self.audit_path:
            try:
                append_json(self.audit_path, {"ts_ns": now_ns, "kind": kind, **detail})
            except OSError as e:
                self._state_error = f"cannot write the risk audit {self.audit_path}: {e}"

    # -- the pre-trade check

    def check(self, intent: Intent, book: dict[str, Exposure], *, live_orders: int, equity: float | None) -> str | None:
        """None if the order may go to NautilusTrader's RiskEngine, else "<code>: <why>"."""
        if self.state is TradingState.HALTED:
            return f"halted: {self.reason}"
        if intent.quantity <= 0:
            return "invalid: quantity must be positive"
        exp = book.get(intent.instrument, Exposure())
        if self._reduces(intent, exp):
            return None
        if self.state is TradingState.REDUCING:
            return f"reducing_only: {self.reason}"
        if equity is None:
            return "no_equity: no account, or an open position without a price; the daily loss cap cannot be checked"
        if self._day is None or self._day.day != self._today:
            return "no_equity: no start-of-day equity yet"
        if live_orders + 1 > self.limits.max_live_orders:
            return f"live_orders: {live_orders} live orders, the cap is {self.limits.max_live_orders}"
        unit, ccy = exp.unit_notional, exp.currency
        if unit is None:
            unit, ccy = intent.limit_unit_notional, intent.limit_currency
        if unit is None:
            return f"no_price: no price for {intent.instrument}"
        if ccy != self.limits.currency:
            return f"currency: {intent.instrument} is valued in {ccy}, the limits are in {self.limits.currency}"
        buy = intent.quantity if intent.side == "BUY" else 0.0
        position = exp.worst_case(buy, intent.quantity - buy) * unit
        if position > self.limits.max_position_notional:
            return (
                f"position_limit: {intent.instrument} worst-case position {position:.2f} {ccy} would exceed "
                f"{self.limits.max_position_notional:.2f}"
            )
        gross = position
        for iid, other in book.items():
            if iid == intent.instrument or not other.worst_case():
                continue
            if other.unit_notional is None or other.currency != self.limits.currency:
                return f"no_price: cannot value the {iid} position in {self.limits.currency}"
            gross += other.worst_case() * other.unit_notional
        if gross > self.limits.max_gross_notional:
            return f"gross_limit: worst-case gross {gross:.2f} {ccy} would exceed {self.limits.max_gross_notional:.2f}"
        return None

    @staticmethod
    def _reduces(intent: Intent, exp: Exposure) -> bool:
        if intent.reduce_only:
            return True  # NautilusTrader's RiskEngine and the venue refuse a reduce-only order that would add
        if intent.side == "SELL":
            return exp.net > 0 and intent.quantity + exp.live_sell + exp.reducing_sell <= exp.net + 1e-12
        return exp.net < 0 and intent.quantity + exp.live_buy + exp.reducing_buy <= -exp.net + 1e-12

    def record_denial(self, now_ns: int, strategy: str, intent: Intent, reason: str):
        code = reason.split(":")[0]
        self.denials[code] = self.denials.get(code, 0) + 1
        key = (strategy, intent.instrument, intent.side, code)
        if key != self._last_denial:  # a strategy retrying the same order is counted, not re-logged
            self._last_denial = key
            self._audit(now_ns, "denied", {"strategy": strategy, **asdict(intent), "reason": reason})


# ----------------------------------------------------------------------------- the strategy side, on NautilusTrader


class RiskManagedStrategy(Strategy):
    """Base class for every lab strategy. Orders pass `self.risk` first; denied ones never reach the engine and
    call `on_risk_denied`. Every second, and before every order, the shared RiskManager is updated; while it is
    REDUCING this strategy's non-reducing orders are cancelled and its positions exited (`on_risk_flatten`), while
    HALTED all its orders are cancelled. Subclasses override on_start freely: the risk timer is installed first."""

    def __new__(cls, config=None, *_args, **_kwargs):
        return super().__new__(cls, config)  # NautilusTrader's Strategy.__new__ takes only the config

    def __init__(self, config, risk: RiskManager):
        super().__init__(config)
        if not isinstance(risk, RiskManager):
            raise TypeError("a RiskManagedStrategy needs the trader's RiskManager")
        self.risk = risk
        self._risk_timer = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        own = cls.__dict__.get("on_start")
        if own is not None:

            @functools.wraps(own)
            def on_start(self):
                self._start_risk_timer()
                own(self)

            cls.on_start = on_start

    def on_start(self):
        self._start_risk_timer()

    def _start_risk_timer(self):
        if not self._risk_timer:
            self._risk_timer = True
            self.clock.set_timer(f"risk-{self.strategy_id}", CHECK_INTERVAL, callback=self._on_risk_timer)

    # -- gated order commands

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> bool:
        reason = self._risk_check([order])
        if reason:
            self._deny(order, reason)
            return False
        super().submit_order(order, position_id, client_id, params)
        self.risk.passed[order.client_order_id] = None
        return True

    def submit_order_list(self, order_list, position_id=None, client_id=None, params=None) -> bool:
        reason = self._risk_check(list(order_list.orders))
        if reason:
            for order in order_list.orders:
                self._deny(order, reason)
            return False
        super().submit_order_list(order_list, position_id, client_id, params)
        for order in order_list.orders:
            self.risk.passed[order.client_order_id] = None
        return True

    def modify_order(
        self, client_order_id, quantity=None, price=None, trigger_price=None, client_id=None, params=None
    ) -> bool:
        order = self.cache.order(client_order_id)
        if order is None:
            reason = f"invalid: unknown order {client_order_id}"
        elif quantity is not None and float(quantity) > float(order.quantity):
            reason = self._risk_check([order], extra=float(quantity) - float(order.quantity))
        else:
            reason = self._risk_check([])
            if reason is None and self.risk.state is TradingState.HALTED:
                reason = f"halted: {self.risk.reason}"
        if reason:
            self.log.warning(f"risk: modify of {client_order_id} denied: {reason}")
            return False
        super().modify_order(client_order_id, quantity, price, trigger_price, client_id, params)
        return True

    def on_risk_denied(self, order, reason: str):
        """Called for every order the risk manager refuses."""

    def on_risk_flatten(self, position):
        """Exit `position` while the risk manager is reducing. Default: reduce-only limits at the opposite touch,
        immediate-or-cancel, each under the per-order notional cap (retried every second while the position is
        open); a market order when there is no book."""
        iid = position.instrument_id
        top = self.cache.top_of_book(iid)
        if top is None:
            self.close_position(position, reduce_only=True)
            return
        sell = position.signed_qty > 0
        price = top[0] if sell else top[2]
        inst = self.cache.instrument(iid)
        unit, _ = self._unit_notional(iid, price)
        left = float(position.quantity)
        chunk = left if not unit else max(self.risk.limits.max_order_notional / unit, float(inst.size_increment))
        while left > 0:
            qty = inst.make_qty(min(chunk, left), round_down=True)
            if not qty.as_double():
                break
            left -= qty.as_double()
            order = self.order_factory.limit(
                iid,
                OrderSide.SELL if sell else OrderSide.BUY,
                qty,
                price,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )
            self.submit_order(order)

    # -- internals

    def _deny(self, order, reason: str):
        self.risk.record_denial(self.clock.timestamp_ns(), str(self.strategy_id), self._intent(order), reason)
        self.log.warning(f"risk: {order.side.name} {order.quantity} {order.instrument_id} denied: {reason}")
        self.on_risk_denied(order, reason)

    def _risk_check(self, orders, *, extra: float | None = None) -> str | None:
        """extra: check a quantity increase of one live order instead of new orders."""
        try:
            self._risk_update()
            book, live = self._exposures()
            equity = self._equity_or_none()
            if extra is not None:
                live -= 1  # the order being modified is already live
            for order in orders:
                intent = self._intent(order, extra)
                reason = self.risk.check(intent, book, live_orders=live, equity=equity)
                if reason:
                    return reason
                if not intent.reduce_only:  # later orders of a list see the earlier ones as live
                    exp = book.setdefault(intent.instrument, Exposure())
                    if intent.side == "BUY":
                        exp.live_buy += intent.quantity
                    else:
                        exp.live_sell += intent.quantity
                    live += 1
            return None
        except Exception as e:  # NautilusTrader swallows callback exceptions: never let one become a pass
            return f"error: {type(e).__name__}: {e}"

    def _intent(self, order, quantity: float | None = None) -> Intent:
        iid = order.instrument_id
        unit = ccy = None
        if order.has_price:
            unit, ccy = self._unit_notional(iid, order.price)
        return Intent(
            instrument=str(iid),
            side=order.side.name,
            quantity=float(order.leaves_qty if quantity is None else quantity),
            reduce_only=bool(order.is_reduce_only),
            limit_unit_notional=unit,
            limit_currency=ccy,
        )

    def _reference_price(self, iid):
        mark = self.cache.mark_price(iid)
        if mark is not None:
            return mark.value
        top = self.cache.top_of_book(iid)
        if top is not None:
            return Price((top[0].as_double() + top[2].as_double()) / 2, top[0].precision + 1)
        for kind in (PriceType.MID, PriceType.LAST):
            price = self.cache.price(iid, kind)
            if price is not None:
                return price
        return None

    def _unit_notional(self, iid, price) -> tuple[float | None, str | None]:
        inst = self.cache.instrument(iid)
        if inst is None or price is None:
            return None, None
        money = inst.notional_value(inst.make_qty(1.0), price, use_quote_for_inverse=True)
        return money.as_double(), money.currency.code

    def _exposures(self) -> tuple[dict[str, Exposure], int]:
        """Every instrument with a position or a live order, over all strategies of the trader."""
        book: dict[str, Exposure] = {}
        live = 0
        for order in self._live_orders():
            live += 1
            exp = book.setdefault(str(order.instrument_id), Exposure())
            qty = float(order.leaves_qty)
            buy = order.side == OrderSide.BUY
            if order.is_reduce_only:
                exp.reducing_buy += qty if buy else 0.0
                exp.reducing_sell += 0.0 if buy else qty
            else:
                exp.live_buy += qty if buy else 0.0
                exp.live_sell += 0.0 if buy else qty
        for position in self.cache.positions_open():
            book.setdefault(str(position.instrument_id), Exposure()).net += float(position.signed_qty)
        for iid_str, exp in book.items():
            iid = InstrumentId.from_str(iid_str)
            exp.unit_notional, exp.currency = self._unit_notional(iid, self._reference_price(iid))
        return book, live

    def _live_orders(self) -> list:
        """Live orders of the whole trader: open, in flight or emulated in the cache, plus those this gate passed
        that the engine has not processed yet (still INITIALIZED in the cache). Bounded by the live orders, not by
        the session's order history."""
        orders = {}
        for source in (self.cache.orders_open(), self.cache.orders_inflight(), self.cache.orders_emulated()):
            for order in source:
                orders[order.client_order_id] = order
        for coid in list(self.risk.passed):
            order = self.cache.order(coid)
            if order is None or order.status in _CLOSED:
                del self.risk.passed[coid]
            else:
                orders[coid] = order
        return [o for o in orders.values() if o.status not in _CLOSED]

    def _equity(self) -> float | None:
        """Account balances plus unrealized PnL over the watched venues, in the limits' currency; None if any part
        cannot be valued."""
        ccy = Currency.from_str(self.risk.limits.currency)
        total = 0.0
        for venue in self.risk.venues:
            account = self.portfolio.account(Venue(venue))
            if account is None:
                return None
            balance = account.balance_total(ccy)
            if balance is None:
                return None
            total += balance.as_double()
        for position in self.cache.positions_open():
            if str(position.instrument_id.venue) not in self.risk.venues:
                continue
            price = self._reference_price(position.instrument_id)
            if price is None:
                return None
            pnl = position.unrealized_pnl(price)
            if pnl.currency.code != self.risk.limits.currency:
                return None
            total += pnl.as_double()
        return total

    def _risk_update(self) -> bool:
        return self.risk.update(self.clock.timestamp_ns(), self._equity_or_none())

    def _equity_or_none(self) -> float | None:
        try:
            return self._equity()
        except Exception:
            return None

    def _on_risk_timer(self, _event):
        changed = self._risk_update()
        if changed:
            log = self.log.info if self.risk.state is TradingState.ACTIVE else self.log.warning
            log(f"risk: {self.risk.state.value} {self.risk.reason}")
        if self.risk.state is TradingState.ACTIVE:
            return
        for order in self.cache.orders_open(strategy_id=self.strategy_id):
            halted = self.risk.state is TradingState.HALTED
            if (halted or not order.is_reduce_only) and not order.is_pending_cancel:
                self.cancel_order(order.client_order_id)
        if self.risk.state is TradingState.REDUCING:
            for position in self.cache.positions_open(strategy_id=self.strategy_id):
                pending = [
                    o
                    for o in self._live_orders()
                    if o.is_reduce_only
                    and o.strategy_id == self.strategy_id
                    and o.instrument_id == position.instrument_id
                ]
                if not pending:
                    self.on_risk_flatten(position)


def add_strategy(target, strategy, risk: RiskManager):
    """Add a strategy to a BacktestEngine or LiveNode only if it trades through this trader's risk manager."""
    if not isinstance(strategy, RiskManagedStrategy):
        raise TypeError(f"{type(strategy).__name__} is not a RiskManagedStrategy: every strategy goes through risk")
    if strategy.risk is not risk:
        raise ValueError(f"{type(strategy).__name__} uses another RiskManager than this trader's")
    target.add_strategy(strategy)
