"""IBKR PAPER London Gold strategy, sharing the stock node's account and controls."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from nautilus_trader.model import (
    BarType,
    ClientOrderId,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.gold import GoldBar, plan_gold_trade
from lab.halt import read_halt, set_halt
from lab.persistence import atomic_json, read_json
from lab.safety import is_paper_account, read_kill_switch

GOLD = InstrumentId.from_str("XAUUSD.IBCMDTY")
FX = InstrumentId.from_str("EUR/USD.IDEALPRO")
GOLD_DAILY = BarType.from_str(f"{GOLD}-1-DAY-MID-EXTERNAL")
GOLD_MINUTE = BarType.from_str(f"{GOLD}-1-MINUTE-MID-EXTERNAL")
FX_MINUTE = BarType.from_str(f"{FX}-1-MINUTE-MID-EXTERNAL")
MAX_QUOTE_AGE = timedelta(minutes=30)
MAX_GOLD_CLOSE_AGE = timedelta(hours=30)
MAX_ENTRY_AGE = timedelta(minutes=30)
ENTRY_COLLAR = Decimal("0.01")


class GoldPaperConfig(StrategyConfig):
    def __init__(
        self,
        *,
        paper_account: str,
        host: str,
        port: int,
        data_client_id: int,
        runtime: str,
        capital_usd: float,
        max_order_value: float,
        authority,
        data_delay_min: float,
        notification_health=None,
        **_kwargs,
    ):
        super().__init__()
        self.paper_account = paper_account
        self.host = host
        self.port = port
        self.data_client_id = data_client_id
        self.runtime = Path(runtime)
        self.capital_usd = Decimal(str(capital_usd))
        self.max_order_value = Decimal(str(max_order_value))
        self.authority = authority
        self.data_delay = timedelta(minutes=data_delay_min)
        self.notification_health = notification_health


class GoldPaperStrategy(Strategy):
    """One daily long bracket at most, with a persistent intent before submission.

    Gold and stocks share the same Nautilus live node, account, runtime lock,
    kill switch, hard halt, authority, and IB execution client. The stock ledger
    does not include this commodity; this strategy owns its own broker position.
    """

    def __init__(self, config: GoldPaperConfig):
        super().__init__(config)
        self.bars: list[GoldBar] = []
        self.gold_quote = None
        self.gold_quote_at = None
        self.fx_quote = None
        self.fx_quote_at = None
        self.cycle = False
        self.fetch_task = None
        self.received: set[str] = set()
        self.data_ready = False
        self.state_path = config.runtime / "gold-state.json"
        self.status_path = config.runtime / "gold-status.json"
        self.last_submitted = None
        self.emergency_order_id = None
        self.approval_pending = False
        self.known_order_ids: set[str] = set()
        self.emergency_exit = False
        if self.state_path.exists():
            state = read_json(self.state_path)
            self.last_submitted = state.get("last_submitted_session")
            self.emergency_order_id = state.get("emergency_order_id")
            self.known_order_ids = set(state.get("order_ids", []))

    def on_start(self):
        if self.config.authority is None:
            raise ValueError("gold requires operator authority")
        self.config.authority.read()
        history = read_json(self.config.runtime / "gold-history.json")
        if history.get("source") != "IBKR-XAUUSD-CMDTY-SMART-MID":
            raise ValueError("unverified gold history source")
        self.bars = [
            GoldBar(
                (datetime.fromisoformat(row["end"]) - timedelta(microseconds=1)).date(),
                Decimal(row["open"]),
                Decimal(row["high"]),
                Decimal(row["low"]),
                Decimal(row["close"]),
            )
            for row in history["bars"]
        ]
        if len(self.bars) < 201:
            raise ValueError("gold history has fewer than 201 closed bars")
        self.data_ready = True
        self._schedule()
        self._schedule_monitor(45)
        self._status("gold data ready", bars=len(self.bars))

    def _schedule_monitor(self, delay):
        at = datetime.now(UTC) + timedelta(seconds=delay)
        self.clock.set_time_alert_ns(
            f"gold-monitor-{int(at.timestamp() * 1e9)}", int(at.timestamp() * 1e9), callback=self._monitor
        )

    def _persist_state(self):
        atomic_json(
            self.state_path,
            {
                "last_submitted_session": self.last_submitted,
                "emergency_order_id": self.emergency_order_id,
                "order_ids": sorted(self.known_order_ids),
            },
        )

    def _monitor(self, _event):
        self._schedule_monitor(60)
        if not self._paper_account_ok():
            return
        position = Decimal(str(self.portfolio.net_position(GOLD)))
        all_orders = self.cache.orders_open(instrument_id=GOLD)
        orders = [o for o in all_orders if str(o.client_order_id) in self.known_order_ids]
        positions = self.cache.positions_open(instrument_id=GOLD)
        if position == 0 and self.emergency_order_id:
            resolved = self.cache.order(ClientOrderId(self.emergency_order_id))
            if resolved is not None and resolved.is_closed:
                self.emergency_order_id = None
                self._persist_state()
        if any(str(o.client_order_id) not in self.known_order_ids for o in all_orders) or (
            position != 0
            and (not positions or any(str(p.opening_order_id) not in self.known_order_ids for p in positions))
        ):
            set_halt(self.config.runtime / "HALT", "reconciliation_mismatch", "gold position or order not owned by lab")
            self._status("foreign gold position or order requires operator review")
            return
        now_ns = self.clock.timestamp_ns()
        for order in orders:
            if order.side == OrderSide.BUY and now_ns - order.ts_init > MAX_ENTRY_AGE.total_seconds() * 1e9:
                self.cancel_order(order.client_order_id)
        kill = read_kill_switch(self.config.runtime / "KILL")
        if kill and kill.get("mode") == "flatten":
            self.emergency_exit = True
        if position < 0 and not self.emergency_exit:
            self.emergency_exit = True
            set_halt(self.config.runtime / "HALT", "reconciliation_mismatch", "unexpected short gold position")
        if position == 0 and orders and not any(o.side == OrderSide.BUY for o in orders):
            set_halt(self.config.runtime / "HALT", "reconciliation_mismatch", "orphaned gold exit order while flat")
            for order in orders:
                self.cancel_order(order.client_order_id)
            self._status("canceling orphaned gold exits")
            return
        if position > 0 and not self.emergency_exit:
            stops = [o for o in orders if o.side == OrderSide.SELL and o.order_type == OrderType.STOP_MARKET]
            targets = [o for o in orders if o.side == OrderSide.SELL and o.order_type == OrderType.LIMIT]
            if (
                sum((Decimal(str(o.quantity)) for o in stops), Decimal(0)) < position
                or sum((Decimal(str(o.quantity)) for o in targets), Decimal(0)) < position
            ):
                self.emergency_exit = True
                set_halt(
                    self.config.runtime / "HALT", "reconciliation_mismatch", "gold position lacks bracket protection"
                )
        if self.emergency_exit:
            closing_side = OrderSide.SELL if position > 0 else OrderSide.BUY
            closing = [o for o in orders if o.side == closing_side and o.order_type == OrderType.MARKET]
            other = [o for o in orders if o not in closing]
            if other:
                for order in other:
                    self.cancel_order(order.client_order_id)
                self._status("canceling gold orders before emergency exit", position_oz=float(position))
            elif closing:
                self._status("waiting for emergency gold exit fill", position_oz=float(position))
            elif position != 0:
                if self.emergency_order_id:
                    self._status("emergency gold exit unresolved; operator review", position_oz=float(position))
                    return
                if abs(position) != int(abs(position)):
                    self._status("gold position size requires operator exit", position_oz=float(position))
                    return
                exit_order = self.order_factory.market(
                    GOLD, closing_side, Quantity.from_int(int(abs(position))), time_in_force=TimeInForce.DAY
                )
                self.known_order_ids.add(str(exit_order.client_order_id))
                self.emergency_order_id = str(exit_order.client_order_id)
                self._persist_state()
                self.submit_order(exit_order)
                self._status("closing unprotected gold position", position_oz=float(position))
            else:
                self.emergency_exit = False
                self._status("gold flat after emergency control")
            return
        if kill or read_halt(self.config.runtime / "HALT"):
            for order in orders:
                if position == 0 or order.side == OrderSide.BUY:
                    self.cancel_order(order.client_order_id)
            self._status("gold paused", position_oz=float(position), open_orders=len(orders))
            return
        if position != 0:
            self._status("gold position open", position_oz=float(position), open_orders=len(orders))
        elif not orders:
            if self.approval_pending:
                self._decide()
                return
            self._status("gold flat", position_oz=0, open_orders=0)

    def _schedule(self):
        now = datetime.now(UTC)
        next_cycle = now.replace(hour=0, minute=20, second=0, microsecond=0)
        if next_cycle <= now:
            next_cycle += timedelta(days=1)
        self.clock.set_time_alert_ns(
            f"gold-cycle-{next_cycle:%Y%m%d}", int(next_cycle.timestamp() * 1e9), callback=self._daily_cycle
        )

    def _daily_cycle(self, _event):
        self._schedule()
        if read_kill_switch(self.config.runtime / "KILL") or read_halt(self.config.runtime / "HALT"):
            self._status("paused by control")
            return
        if self.emergency_order_id:
            self._status("emergency gold exit requires reconciliation")
            return
        self.approval_pending = False
        self.cycle = True
        self.received.clear()
        self.fetch_task = asyncio.create_task(self._download_cycle())

    def on_stop(self):
        self.cycle = False
        if self.fetch_task is not None:
            self.fetch_task.cancel()

    async def _download_cycle(self):
        from lab.gold_data import download_gold_cycle

        try:
            snapshot = await download_gold_cycle(
                self.config.host, self.config.port, self.config.data_client_id, self.config.paper_account
            )
            if not self.is_running():
                return
            for bars in snapshot:
                self.on_historical_bars(bars)
            if self.cycle:
                self.cycle = False
                self._status("gold or FX broker data unavailable")
        except Exception as exc:
            self.cycle = False
            self._status(f"gold data request failed: {type(exc).__name__}: {exc}")

    def on_historical_bars(self, bars):
        if not bars:
            return
        bar_type = bars[0].bar_type
        now = datetime.now(UTC)
        if bar_type == GOLD_DAILY:
            complete = [b for b in bars if b.ts_event / 1e9 + self.config.data_delay.total_seconds() <= now.timestamp()]
            recent = [
                GoldBar(
                    (datetime.fromtimestamp(b.ts_event / 1e9, UTC) - timedelta(microseconds=1)).date(),
                    Decimal(str(b.open)),
                    Decimal(str(b.high)),
                    Decimal(str(b.low)),
                    Decimal(str(b.close)),
                )
                for b in complete
            ]
            merged = {bar.session: bar for bar in self.bars}
            merged.update({bar.session: bar for bar in recent})
            self.bars = [merged[day] for day in sorted(merged)][-260:]
            self.received.add("daily")
            if len(self.bars) >= 201:
                self.data_ready = True
                self._status("gold data ready", bars=len(self.bars))
        elif bar_type == GOLD_MINUTE:
            self.gold_quote, self.gold_quote_at = self._latest_quote(bars, now)
            self.received.add("gold_quote")
        elif bar_type == FX_MINUTE:
            self.fx_quote, self.fx_quote_at = self._latest_quote(bars, now)
            self.received.add("fx_quote")
        if self.cycle and self.received == {"daily", "gold_quote", "fx_quote"}:
            self.cycle = False
            self._decide()

    @staticmethod
    def _latest_quote(bars, now):
        eligible = [b for b in bars if 0 <= (now.timestamp() - b.ts_event / 1e9) <= MAX_QUOTE_AGE.total_seconds()]
        if not eligible:
            return None, None
        bar = max(eligible, key=lambda b: b.ts_event)
        return Decimal(str(bar.close)), datetime.fromtimestamp(bar.ts_event / 1e9, UTC)

    def _authorize_order(self, order):
        """Authorize against the mandate which is active at decision time.

        The app can renew the mandate after this strategy starts. Passing the
        freshly read nonce still protects against a concurrent mandate change,
        without pinning the strategy to the startup mandate forever.
        """
        mandate = self.config.authority.read()
        return self.config.authority.authorize(order, expected_nonce=mandate["nonce"])

    def _paper_account_ok(self):
        account = self.cache.account_id(Venue("IB"))
        actual = str(account) if account is not None else ""
        if not is_paper_account(actual) or actual.split("-", 1)[-1] != self.config.paper_account:
            set_halt(self.config.runtime / "HALT", "paper_guard", "gold: broker PAPER account mismatch")
            self._status("paper account mismatch")
            return False
        return True

    def _equity_usd(self):
        account = self.cache.account_for_venue(Venue("IB"))
        balance = account.balance_total() if account is not None else None
        if balance is None:
            return None
        amount = Decimal(str(balance.as_double()))
        currency = balance.currency.code
        if currency == "USD":
            return amount
        if currency == "EUR" and self.fx_quote is not None and self.fx_quote > 0:
            return amount * self.fx_quote
        return None

    def _decide(self):
        self.approval_pending = False
        if not self._paper_account_ok():
            return
        if read_kill_switch(self.config.runtime / "KILL") or read_halt(self.config.runtime / "HALT"):
            self._status("paused by control")
            return
        if self.config.notification_health is not None and not self.config.notification_health()["healthy"]:
            self._status("notification delivery unavailable")
            return
        if not self.bars or self.gold_quote is None or self.fx_quote is None:
            self._status("gold or FX history unavailable")
            return
        now = datetime.now(UTC)
        if (
            self.gold_quote_at is None
            or self.fx_quote_at is None
            or now - self.gold_quote_at > MAX_QUOTE_AGE
            or now - self.fx_quote_at > MAX_QUOTE_AGE
        ):
            self._status("gold or FX quote stale")
            return
        last_close = datetime.combine(self.bars[-1].session + timedelta(days=1), datetime.min.time(), UTC)
        if now - last_close > MAX_GOLD_CLOSE_AGE:
            self._status("gold daily bar stale")
            return
        instrument = self.cache.instrument(GOLD)
        if instrument is None or instrument.min_quantity is None or instrument.size_increment is None:
            self._status("broker gold contract limits unavailable")
            return
        minimum = int(str(instrument.min_quantity))
        if minimum < 1 or Decimal(str(instrument.size_increment)) != 1:
            self._status("unsupported broker gold lot size")
            return
        if self.portfolio.net_position(GOLD) != 0 or self.cache.orders_open_count(instrument_id=GOLD):
            self._status("existing gold position or order")
            return
        broker_equity = self._equity_usd()
        if broker_equity is None or broker_equity <= 0:
            self._status("verified account equity unavailable")
            return
        equity = min(broker_equity, self.config.capital_usd)
        tick = Decimal(str(instrument.price_increment))
        try:
            plan = plan_gold_trade(self.bars, equity_usd=equity, price_tick_usd=tick, min_quantity_oz=minimum)
        except ValueError as exc:
            self._status(f"gold plan invalid: {exc}")
            return
        if plan is None:
            self._status("no gold entry signal")
            return
        if self.last_submitted is not None and plan.session.isoformat() <= self.last_submitted:
            self._status("gold signal already acted on")
            return
        # The minute quote must still confirm the close. A limit entry may fill
        # below its cap; levels use the cap, so actual reward/risk is at least 2:1.
        if not (plan.reference_usd * (1 - ENTRY_COLLAR) <= self.gold_quote <= plan.reference_usd * (1 + ENTRY_COLLAR)):
            self._status("current gold price moved beyond entry collar")
            return
        entry_limit = ((self.gold_quote * (1 + ENTRY_COLLAR)) / tick).to_integral_value(rounding=ROUND_UP) * tick
        quantity = max(minimum, int(plan.allocated_usd // entry_limit))
        if quantity * entry_limit > self.config.max_order_value:
            self._status("gold order exceeds hard value limit")
            return
        if self.config.capital_usd + quantity * entry_limit > broker_equity:
            self._status("paper account has no separate capacity for gold")
            return
        stop = ((self.gold_quote - 2 * plan.atr_usd) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        target = ((entry_limit + 2 * (entry_limit - stop)) / tick).to_integral_value(rounding=ROUND_UP) * tick
        if stop <= 0 or not (stop < self.gold_quote < entry_limit < target):
            self._status("invalid gold protection levels")
            return
        order = {
            "ticker": "XAUUSD",
            "side": "BUY",
            "qty": quantity,
            "decision_price": float(self.gold_quote),
            "entry_limit": float(entry_limit),
            "stop": float(stop),
            "target": float(target),
            "minimum_lot_override": quantity * entry_limit > plan.allocated_usd,
        }
        try:
            if not self._authorize_order(order):
                self.approval_pending = True
                self._status("gold order awaiting operator approval")
                return
        except (OSError, ValueError) as exc:
            self._status(f"gold authority unavailable: {exc}")
            return
        if (
            not self._paper_account_ok()
            or read_kill_switch(self.config.runtime / "KILL")
            or read_halt(self.config.runtime / "HALT")
        ):
            return
        bracket = self.order_factory.bracket(
            instrument_id=GOLD,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(quantity),
            entry_order_type=OrderType.LIMIT,
            entry_price=Price.from_str(str(entry_limit)),
            time_in_force=TimeInForce.GTC,
            tp_price=Price.from_str(str(target)),
            tp_post_only=False,
            sl_trigger_price=Price.from_str(str(stop)),
        )
        self.last_submitted = plan.session.isoformat()
        self.known_order_ids = {str(o.client_order_id) for o in bracket}
        self._persist_state()
        self.submit_order_list(bracket)
        self._status("gold bracket submitted", **order)

    def _status(self, state, **details):
        atomic_json(
            self.status_path,
            {
                "at": datetime.now(UTC).isoformat(),
                "session": os.environ.get("LAB_APP_SESSION"),
                "state": state,
                "ready": self.data_ready,
                **details,
            },
        )

    def on_order_denied(self, event):
        self._order_failed(event)

    def on_order_rejected(self, event):
        self._order_failed(event)

    def _order_failed(self, event):
        if event.instrument_id != GOLD:
            return
        set_halt(self.config.runtime / "HALT", "reconciliation_mismatch", "gold bracket leg rejected")
        self.emergency_exit = True
        self._status("gold order rejected; emergency exit pending")
        self._schedule_monitor(5)
