"""
PassiveMaker: quote both sides of the book with post-only limit orders and earn the spread, the strategy
`lab maker-study` measures. Paper only, like every lab strategy: it runs in the backtest on recorded data and on the
sandbox paper node (lab.fast), never against an exchange account.

On every complete book update it wants one order per side:
- at the touch, or improve_ticks inside the spread when that still leaves it short of the opposite touch;
- of quote_size, cut to what keeps the position within max_inventory (base units) either way;
- only while the book imbalance does not lean against that side by more than min_favour allows (the study splits
  fills by this: a bid quoted into a heavy offer is the one that gets picked off);
- post-only, so it can never take liquidity by accident.
A live order whose price is no longer the wanted one is cancelled and replaced on a later update once the cancel is
done; a side with nothing wanted is cancelled.

Inventory that has been open longer than max_hold_s is exited at the touch (immediate-or-cancel): a maker that
waits for the market to come back is a directional trader nobody asked for.

Guards: a RiskManagedStrategy through lab.fast, so every order passes the trader's risk manager (kill switch, daily
loss cap that flattens and stops new risk, notional and order-rate limits) and then NautilusTrader's RiskEngine.
While the risk manager is not ACTIVE it quotes nothing and cancels its quotes.
"""

from __future__ import annotations

from nautilus_trader.model import OrderSide, TimeInForce
from nautilus_trader.trading import StrategyConfig

from lab.fast import F_LAST, FastStrategy
from lab.micro import IMBALANCE_LEVELS, book_imbalance
from lab.risk_manager import TradingState


class PassiveMakerConfig(StrategyConfig):
    def __init__(
        self,
        *,
        instrument_id: str,
        quote_size: float,
        max_inventory: float | None = None,
        improve_ticks: int = 0,
        min_favour: float = -1.0,
        max_hold_s: float = 60.0,
        report: str | None = None,
        report_s: float = 5.0,
        **_kwargs,
    ):
        """quote_size and max_inventory (default: 3 x quote_size) in base units. min_favour in [-1, 1]: quote a side
        only while side x book imbalance >= it (-1: always). report: see ImbalanceTakerConfig."""
        super().__init__()
        max_inventory = 3 * quote_size if max_inventory is None else max_inventory
        if not quote_size > 0 or not max_inventory >= quote_size:
            raise ValueError("quote_size must be positive and max_inventory at least quote_size")
        if improve_ticks < 0 or not -1 <= min_favour <= 1 or not max_hold_s > 0:
            raise ValueError("improve_ticks >= 0, min_favour in [-1, 1] and max_hold_s > 0")
        self.instrument_id = instrument_id
        self.quote_size = quote_size
        self.max_inventory = max_inventory
        self.improve_ticks = improve_ticks
        self.min_favour = min_favour
        self.max_hold_s = max_hold_s
        self.report = report
        self.report_s = report_s


SIDES = ((OrderSide.BUY, 1), (OrderSide.SELL, -1))


class PassiveMaker(FastStrategy):
    def params(self) -> dict:
        c = self.config
        return {
            "quote_size": c.quote_size,
            "max_inventory": c.max_inventory,
            "improve_ticks": c.improve_ticks,
            "min_favour": c.min_favour,
            "max_hold_s": c.max_hold_s,
        }

    def _quotes(self) -> dict:
        """This strategy's live quotes (not exits) by side."""
        out = {}
        for o in self.cache.orders_open(instrument_id=self.iid, strategy_id=self.strategy_id):
            if not o.is_reduce_only:
                out.setdefault(o.side, []).append(o)
        for o in self.cache.orders_inflight(instrument_id=self.iid, strategy_id=self.strategy_id):
            if not o.is_reduce_only:
                out.setdefault(o.side, []).append(o)
        return out

    def _cancel_quotes(self):
        for orders in self._quotes().values():
            for o in orders:
                if o.is_open and not o.is_pending_cancel:
                    self.cancel_order(o.client_order_id)

    def wanted(self, bids, asks, net: float) -> dict:
        """{side: (price, quantity)} the strategy wants resting now, from the book's top levels (price, size) and
        the net position."""
        inst = self.cache.instrument(self.iid)
        tick = float(inst.price_increment)
        imb = book_imbalance(bids, asks)
        out = {}
        for side, sign in SIDES:
            room = self.config.max_inventory - sign * net
            qty = min(self.config.quote_size, room)
            if qty < float(inst.size_increment):
                continue
            if imb is not None and sign * imb.imbalance < self.config.min_favour:
                continue
            touch, opposite = (bids[0][0], asks[0][0]) if sign > 0 else (asks[0][0], bids[0][0])
            price = touch
            if self.config.improve_ticks:
                inside = round(touch + sign * self.config.improve_ticks * tick, 12)
                if sign * (opposite - inside) > 0:
                    price = inside
            out[side] = (price, qty)
        return out

    def on_book_deltas(self, deltas):
        if not deltas.flags & F_LAST:
            return
        book = self.cache.order_book(self.iid)
        if book is None:
            return
        if self.risk.state is not TradingState.ACTIVE:
            if self.stopped_reason is None:
                self.stopped_reason = self.risk.reason
            self._cancel_quotes()  # the risk timer exits the position
            return
        net = float(self.portfolio.net_position(self.iid))
        if net:
            opened = [p.ts_opened for p in self.cache.positions_open(instrument_id=self.iid)]
            if opened and self.clock.timestamp_ns() - min(opened) >= self.config.max_hold_s * 1e9:
                self._cancel_quotes()
                if not self._exiting():
                    self._close()
                return
        bids = [(float(lv.price), lv.size()) for lv in book.bids(IMBALANCE_LEVELS)]
        asks = [(float(lv.price), lv.size()) for lv in book.asks(IMBALANCE_LEVELS)]
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return
        wanted = self.wanted(bids, asks, net)
        live = self._quotes()
        inst = self.cache.instrument(self.iid)
        for side, _ in SIDES:
            orders = live.get(side, [])
            want = wanted.get(side)
            if orders:
                for o in orders:
                    stale = want is None or float(o.price) != want[0] or float(o.leaves_qty) > want[1] + 1e-12
                    if stale and o.is_open and not o.is_pending_cancel:
                        self.cancel_order(o.client_order_id)
                continue  # replace once the old quote is gone
            if want is None:
                continue
            order = self.order_factory.limit(
                self.iid,
                side,
                inst.make_qty(want[1]),
                inst.make_price(want[0]),
                time_in_force=TimeInForce.GTC,
                post_only=True,
            )
            if self.start_equity is None:
                self.start_equity = self._equity_or_none()
            self.entry_orders.add(str(order.client_order_id))
            if not self.submit_order(order):
                self.entry_orders.discard(str(order.client_order_id))

    def _exiting(self) -> bool:
        return any(o.is_reduce_only for o in self._live_orders() if o.strategy_id == self.strategy_id)
