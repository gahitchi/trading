"""Offline, deterministic linear-contract reference simulator (NO network or live routing).

This is a deliberately narrow accounting/risk oracle for early multi-market
development. It is NOT an order book or broker-quality execution simulator:
market fills use the next observed trade with explicit adverse slippage.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from lab.markets.domain import (
    AssetClass,
    Capabilities,
    ContractKind,
    Instrument,
    OrderIntent,
    Side,
    Trade,
    TradingMode,
    UnsupportedMode,
    decimal,
    positive,
)
from lab.markets.journal import EventJournal, JournalError

DAY_NS = 86_400_000_000_000


@dataclass(frozen=True, slots=True)
class SimulationLimits:
    max_gross: Decimal = Decimal("1.8")
    max_position_fraction: Decimal = Decimal("0.75")
    initial_margin_buffer: Decimal = Decimal("0.15")
    max_drawdown: Decimal = Decimal("0.1")
    max_daily_loss: Decimal = Decimal("0.05")
    max_order_notional: Decimal = Decimal("1000")
    participation: Decimal = Decimal("0.1")

    def __post_init__(self) -> None:
        for name in ("max_gross", "max_position_fraction", "max_order_notional", "participation"):
            positive(decimal(getattr(self, name)), name)
        for name in ("initial_margin_buffer", "max_drawdown", "max_daily_loss"):
            v = decimal(getattr(self, name))
            if not Decimal("0") < v < Decimal("1"):
                raise ValueError(f"{name} must be in (0, 1)")
        if not Decimal("0") < self.participation <= Decimal("1"):
            raise ValueError("participation must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    trade_id: str
    timestamp_ns: int
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal


@dataclass(slots=True)
class _Pending:
    intent: OrderIntent
    remaining: Decimal


class OfflineSimulator:
    """Local linear-contract reference accounting with next-tick market fills.

    Cash is collateral rather than notional sale proceeds. Funding must be supplied
    as an explicit event. The simulator cannot connect to any external order API.
    """

    def __init__(
        self,
        instrument: Instrument,
        *,
        starting_balance: Decimal = Decimal("1000"),
        limits: SimulationLimits | None = None,
        taker_fee_bps: Decimal = Decimal("5"),
        slippage_bps: Decimal = Decimal("2"),
        mode: TradingMode = TradingMode.OFFLINE_SIM,
        journal: EventJournal | None = None,
    ) -> None:
        if mode != TradingMode.OFFLINE_SIM:
            raise UnsupportedMode("reference simulation refuses BROKER_DEMO and LIVE")
        if instrument.asset_class != AssetClass.CRYPTO or instrument.kind != ContractKind.LINEAR_DERIVATIVE:
            raise ValueError("v1 reference simulator supports crypto linear derivatives only")
        if journal is not None and journal.read_all():
            raise JournalError("refusing to reset a nonempty journal without full state replay")
        self.journal = journal
        self.instrument = instrument
        self.mode = mode
        self.limits = limits or SimulationLimits()
        self.balance = decimal(starting_balance)
        positive(self.balance, "starting balance")
        self.starting_balance = self.balance
        self.taker_fee_bps = decimal(taker_fee_bps)
        self.slippage_bps = decimal(slippage_bps)
        if not Decimal("0") <= self.taker_fee_bps <= Decimal("1000"):
            raise ValueError("invalid taker fee")
        if not Decimal("0") <= self.slippage_bps <= Decimal("1000"):
            raise ValueError("invalid slippage")
        self.capabilities = Capabilities(
            venue=instrument.venue,
            mode=TradingMode.OFFLINE_SIM,
            supported_instruments=frozenset({instrument.instrument_id}),
            may_short=True,
            may_leverage=True,
            may_submit_orders=True,
        )
        self.position = Decimal("0")
        self.entry = Decimal("0")
        self.mark: Decimal | None = None
        self.last_ns = -1
        self.last_event_ns = -1
        self._trade_ids_at_timestamp: set[str] = set()
        self.peak_equity = self.balance
        self.day_index: int | None = None
        self.day_open_equity = self.balance
        self.halt_reason: str | None = None
        self.pending: dict[str, _Pending] = {}
        self.used_ids: set[str] = set()
        self.used_funding_ids: set[str] = set()
        self.fills: list[Fill] = []
        self.rejections: list[tuple[str, str]] = []
        self.total_fees = Decimal("0")
        self.total_funding_paid = Decimal("0")
        self.total_realized = Decimal("0")
        self.last_trade_id: str | None = None

    @property
    def unrealized(self) -> Decimal:
        if self.mark is None or self.position == 0:
            return Decimal("0")
        return (self.mark - self.entry) * self.position * self.instrument.multiplier

    @property
    def equity(self) -> Decimal:
        return self.balance + self.unrealized

    @property
    def exposure(self) -> Decimal:
        return abs(self.position * (self.mark or Decimal("0")) * self.instrument.multiplier)

    def submit(self, intent: OrderIntent) -> None:
        self.capabilities.require(self.instrument, TradingMode.OFFLINE_SIM, short=intent.side == Side.SELL)
        if intent.instrument_id != self.instrument.instrument_id:
            raise ValueError("wrong instrument")
        if intent.client_order_id in self.used_ids:
            raise ValueError("duplicate client order ID, including already filled/cancelled")
        self.instrument.check_quantity(intent.quantity)
        if self.mark is None or intent.created_ns < self.last_ns:
            raise ValueError("order needs a valid mark and non-stale decision timestamp")
        if intent.quantity * self.mark * self.instrument.multiplier < self.instrument.min_notional:
            raise ValueError("order below instrument minimum notional")
        if intent.quantity * self.mark * self.instrument.multiplier > self.limits.max_order_notional:
            raise ValueError("order exceeds operator max order notional")
        change = Decimal(intent.side.sign) * intent.quantity
        if intent.reduce_only and not self._is_reduction(change):
            raise ValueError("reduce-only order may not open or reverse a position")
        if self.halt_reason and not intent.reduce_only:
            raise ValueError("hard halt: only valid reduce-only orders accepted")
        if self.journal is not None:
            self.journal.append(
                f"intent:{intent.client_order_id}",
                "intent",
                intent.created_ns,
                {"instrument": intent.instrument_id, "side": str(intent.side), "quantity": str(intent.quantity),
                 "reduce_only": intent.reduce_only},
            )
        self.used_ids.add(intent.client_order_id)
        self.pending[intent.client_order_id] = _Pending(intent, intent.quantity)

    def cancel(self, order_id: str) -> bool:
        if order_id not in self.pending:
            return False
        if self.journal is not None:
            self.journal.append(f"cancel:{order_id}", "cancel", self.last_event_ns, {"order_id": order_id})
        self.pending.pop(order_id)
        return True

    def _is_reduction(self, delta: Decimal) -> bool:
        return self.position != 0 and self.position * delta < 0 and abs(delta) <= abs(self.position)

    def _next_position(self, delta: Decimal, fill_price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        old = self.position
        new = old + delta
        if old == 0 or old * delta > 0:
            avg = ((abs(old) * self.entry + abs(delta) * fill_price) / abs(new)) if new else Decimal("0")
            return new, avg, Decimal("0")
        closed = min(abs(old), abs(delta))
        realized = (fill_price - self.entry) * closed * self.instrument.multiplier
        if old < 0:
            realized = -realized
        if new == 0:
            avg = Decimal("0")
        elif old * new > 0:
            avg = self.entry
        else:
            avg = fill_price
        return new, avg, realized

    def _price(self, last: Decimal, side: Side) -> Decimal:
        shift = last * self.slippage_bps / Decimal("10000")
        worst = last + shift if side == Side.BUY else last - shift
        positive(worst, "execution price")
        rounding = ROUND_CEILING if side == Side.BUY else ROUND_FLOOR
        ticks = (worst / self.instrument.tick_size).to_integral_value(rounding=rounding)
        return ticks * self.instrument.tick_size

    def _check_risk(
        self,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        realized: Decimal,
        new_pos: Decimal,
        avg: Decimal,
        *,
        reducing: bool,
    ) -> None:
        projected_balance = self.balance + realized - fee
        projected_unreal = (self.mark - avg) * new_pos * self.instrument.multiplier
        projected_equity = projected_balance + projected_unreal
        if reducing:
            return  # emergency reductions are allowed even in a maintenance breach
        if self.halt_reason:
            raise ValueError("hard halt forbids new exposure")
        if projected_equity <= 0:
            raise ValueError("insufficient equity")
        notional = abs(new_pos * self.mark * self.instrument.multiplier)
        max_exposure = min(self.limits.max_gross, self.limits.max_position_fraction) * projected_equity
        if notional > max_exposure:
            raise ValueError("projected exposure exceeds operator risk ceilings")
        required = notional * self.instrument.initial_margin
        if required > projected_equity * (1 - self.limits.initial_margin_buffer):
            raise ValueError("insufficient buffered initial margin")

    def _execute(self, pending: _Pending, trade: Trade) -> Fill | None:
        order = pending.intent
        if trade.exchange_ns <= order.created_ns:
            return None
        tradable = (trade.quantity * self.limits.participation) // self.instrument.quantity_step
        qty = min(pending.remaining, tradable * self.instrument.quantity_step)
        if qty == 0:
            return None
        delta = Decimal(order.side.sign) * qty
        reducing = self._is_reduction(delta)
        if order.reduce_only and not reducing:
            raise ValueError("reduce-only order cannot increase position")
        px = self._price(trade.price, order.side)
        notional = qty * px * self.instrument.multiplier
        fee = notional * self.taker_fee_bps / Decimal("10000")
        new_pos, avg, realized = self._next_position(delta, px)
        self._check_risk(qty, px, fee, realized, new_pos, avg, reducing=reducing)
        if self.journal is not None:
            self.journal.append(
                f"fill:{order.client_order_id}:{trade.trade_id}",
                "fill",
                trade.exchange_ns,
                {"side": str(order.side), "quantity": str(qty), "price": str(px),
                 "fee": str(fee), "realized_pnl": str(realized)},
            )
        self.position = new_pos
        self.entry = avg
        self.balance += realized - fee
        self.total_realized += realized
        self.total_fees += fee
        pending.remaining -= qty
        if pending.remaining == 0:
            self.pending.pop(order.client_order_id, None)
        fill = Fill(order.client_order_id, trade.trade_id, trade.exchange_ns, order.side, qty, px, fee, realized)
        self.fills.append(fill)
        return fill

    def _risk_update(self) -> None:
        eq = self.equity
        self.peak_equity = max(self.peak_equity, eq)
        if eq <= self.peak_equity * (1 - self.limits.max_drawdown):
            self.halt_reason = "peak-to-current drawdown limit breached"
        if eq <= self.day_open_equity * (1 - self.limits.max_daily_loss):
            self.halt_reason = "daily loss limit breached"
        if self.position and eq <= self.exposure * self.instrument.maintenance_margin:
            self.halt_reason = "maintenance margin breached"
        if self.halt_reason:
            for order_id, pending in tuple(self.pending.items()):
                if not pending.intent.reduce_only:
                    self.pending.pop(order_id)
                    self.rejections.append((order_id, self.halt_reason))

    def on_trade(self, trade: Trade) -> tuple[Fill, ...]:
        if trade.exchange_ns < self.last_event_ns:
            raise ValueError("out-of-order trade or funding event timestamp")
        if trade.exchange_ns != self.last_ns:
            self._trade_ids_at_timestamp.clear()
        if trade.trade_id in self._trade_ids_at_timestamp:
            raise ValueError("duplicate trade ID at timestamp")
        self._trade_ids_at_timestamp.add(trade.trade_id)
        self.instrument.check_price(trade.price)
        if self.journal is not None:
            self.journal.append(
                f"trade:{trade.trade_id}",
                "mark",
                trade.exchange_ns,
                {"price": str(trade.price), "quantity": str(trade.quantity),
                 "received_ns": trade.received_ns},
            )
        day = trade.exchange_ns // DAY_NS
        if self.day_index != day:
            self.day_index = day
            self.day_open_equity = self.equity
        self.mark = trade.price
        self.last_ns = trade.exchange_ns
        self.last_event_ns = trade.exchange_ns
        self.last_trade_id = trade.trade_id
        self._risk_update()
        results = []
        for order_id, pending in tuple(self.pending.items()):
            if order_id not in self.pending:
                continue
            try:
                result = self._execute(pending, trade)
            except ValueError as exc:
                self.pending.pop(order_id, None)
                self.rejections.append((order_id, str(exc)))
            else:
                if result is not None:
                    results.append(result)
        self._risk_update()
        return tuple(results)

    def apply_funding(self, funding_id: str, rate: Decimal, timestamp_ns: int) -> Decimal:
        if not funding_id or funding_id in self.used_funding_ids:
            raise ValueError("missing or duplicate funding event ID")
        if not isinstance(timestamp_ns, int) or timestamp_ns < self.last_event_ns:
            raise ValueError("funding event timestamp predates last event")
        rate = decimal(rate)
        if abs(rate) > Decimal("1"):
            raise ValueError("implausible funding rate; input is a fraction, not percent")
        if self.mark is None:
            raise ValueError("no valid market mark for funding")
        payment = self.position * self.mark * self.instrument.multiplier * rate
        if self.journal is not None:
            self.journal.append(
                f"funding:{funding_id}", "funding", timestamp_ns,
                {"rate": str(rate), "payment": str(payment), "position": str(self.position)},
            )
        self.balance -= payment
        self.total_funding_paid += payment
        self.used_funding_ids.add(funding_id)
        self.last_event_ns = timestamp_ns
        self._risk_update()
        return payment

    def report(self) -> dict:
        return {
            "mode": str(self.mode),
            "instrument": self.instrument.instrument_id,
            "balance": str(self.balance),
            "equity": str(self.equity),
            "position": str(self.position),
            "entry_price": str(self.entry),
            "mark_price": str(self.mark) if self.mark is not None else None,
            "unrealized_pnl": str(self.unrealized),
            "realized_pnl": str(self.total_realized),
            "fees": str(self.total_fees),
            "funding_paid": str(self.total_funding_paid),
            "pending_orders": len(self.pending),
            "fills": len(self.fills),
            "rejections": [list(x) for x in self.rejections],
            "hard_halt": self.halt_reason,
            "execution_model": "next_observed_trade_with_adverse_slippage_and_participation",
            "qualified_for_live_trading": False,
        }
