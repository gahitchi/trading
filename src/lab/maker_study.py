"""
What does a passive (maker) fill earn after the price moves that follow it?

Replays recorded order-book deltas and trades (lab record) in receive-time order and keeps one hypothetical resting
order of negligible size at the best bid and one at the best ask:

- placed on the sampling clock (default every 100 ms) when that side has none, at the touch, at the back of the
  queue (everything resting at that price when it is placed is ahead of it), live latency_ms later;
- the queue ahead shrinks with trades at that price and never exceeds what the book still shows there (cancels are
  assumed to come from ahead of it: optimistic);
- filled by an aggressive trade through its price, or at its price once the queue ahead is used up;
- cancelled, and replaced on the next tick, when the touch moves away from it.

For each fill the markout is the move of the mid from the fill price to h seconds later, signed for the maker
(positive = the fill was worth having), and net_bps is that minus the maker fee. at_fill_bps is the half spread a
fill captures, against the last mid with the order still at the touch; adverse selection is how much of it the
later mid takes back. Fills are split by the book imbalance when the order was placed: in the order's favour
(> 0.33: more size on its own side, so price pressure towards it rising for a bid), against (< -0.33) or neutral.

Descriptive research, not a strategy test: no inventory, no position limits, no partial fills, and a fill
does not move the book.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lab.micro import IMBALANCE_LEVELS, book_imbalance
from lab.micro_study import F_LAST, _levels

FAVOUR = 0.33
COLUMNS = ["group", "horizon_s", "fills", "fills_per_h", "at_fill_bps", "markout_bps", "net_bps", "t"]


@dataclass(frozen=True)
class MakerSettings:
    every_ms: int = 100
    latency_ms: int = 50
    horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0, 60.0)
    maker_fee_bps: float = 2.0  # Binance USD-M base tier, approximate; set your own


@dataclass
class _Order:
    side: int  # +1 bid (buys), -1 ask (sells)
    price: float
    queue: float
    live_ns: int
    favour: float  # side x book imbalance when placed
    touch_mid: float  # the mid at the last book update with the order at the touch


def simulate_fills(deltas, trades, settings: MakerSettings):
    """(fills, mids): fills as a DataFrame of ts, side, price, mid, favour; mids as (ts, mid) arrays of every
    complete book update. A fill's mid is the last one with the order still at the touch: the book can show a level
    gone before the trades that emptied it arrive, and the mid by then is already past the order's price."""
    from nautilus_trader.model import AggressorSide, BookType, OrderBook

    fills, mid_ts, mid_px = [], [], []
    if not deltas:
        return pd.DataFrame(columns=["ts", "side", "price", "mid", "favour"]), (np.array([]), np.array([]))
    book = OrderBook(deltas[0].instrument_id, BookType.L2_MBP)
    orders: dict[int, _Order | None] = {1: None, -1: None}
    step, latency = settings.every_ms * 1_000_000, settings.latency_ms * 1_000_000
    next_tick, mid = 0, math.nan
    events = heapq.merge(
        ((d.ts_init, 1, d) for d in sorted(deltas, key=lambda d: d.ts_init)),
        ((t.ts_init, 0, t) for t in sorted(trades, key=lambda t: t.ts_init)),
        key=lambda e: (e[0], e[1]),
    )
    for ts, is_book, ev in events:
        if not is_book:
            hit = 1 if ev.aggressor_side == AggressorSide.SELL else -1  # sellers hit bids, buyers lift asks
            o = orders[hit]
            if o is None:
                continue
            px = float(ev.price)
            if px == o.price:
                o.queue -= float(ev.size)  # trades at its price use up the queue ahead first
                filled = o.queue < 0
            else:
                filled = hit * (o.price - px) > 0  # traded through its price
            if filled and ts >= o.live_ns:
                fills.append((ts, hit, o.price, o.touch_mid, o.favour))
                orders[hit] = None
            continue
        book.apply_delta(ev)
        if not ev.flags & F_LAST:
            continue
        bids, asks = _levels(book.bids(IMBALANCE_LEVELS)), _levels(book.asks(IMBALANCE_LEVELS))
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        mid_ts.append(ts)
        mid_px.append(mid)
        for side, levels in ((1, bids), (-1, asks)):
            o = orders[side]
            if o is None:
                continue
            if side * (levels[0][0] - o.price) > 0:
                orders[side] = None  # the touch moved away: cancel, requote on the next tick
                continue
            if levels[0][0] == o.price:
                o.touch_mid = mid
            o.queue = min(o.queue, next((q for p, q in levels if p == o.price), 0.0))
        if ts < next_tick:
            continue
        next_tick = ts - ts % step + step
        imb = book_imbalance(bids, asks)
        for side, levels in ((1, bids), (-1, asks)):
            if orders[side] is None:
                favour = side * imb.imbalance if imb else math.nan
                orders[side] = _Order(side, levels[0][0], levels[0][1], ts + latency, favour, mid)
    return (
        pd.DataFrame(fills, columns=["ts", "side", "price", "mid", "favour"]),
        (np.array(mid_ts, dtype=np.int64), np.array(mid_px)),
    )


def markouts(fills: pd.DataFrame, mids, horizon_s: float) -> pd.Series:
    """Maker-signed bps from the fill price to the last mid at or before fill + horizon; NaN past the data."""
    ts, px = mids
    if fills.empty:
        return pd.Series(dtype=float)
    target = fills["ts"].to_numpy() + int(horizon_s * 1e9)
    idx = np.searchsorted(ts, target, side="right") - 1
    out = fills["side"].to_numpy() * (px[idx] / fills["price"].to_numpy() - 1) * 10_000
    out[target > ts[-1]] = np.nan
    return pd.Series(out, index=fills.index)


def evaluate(fills: pd.DataFrame, mids, settings: MakerSettings) -> pd.DataFrame:
    if fills.empty or len(mids[0]) < 2:
        return pd.DataFrame(columns=COLUMNS)
    hours = (mids[0][-1] - mids[0][0]) / 3.6e12
    at_fill = fills["side"] * (fills["mid"] / fills["price"] - 1) * 10_000
    group = pd.Series("neutral", index=fills.index)
    group[fills["favour"] > FAVOUR] = "favours"
    group[fills["favour"] < -FAVOUR] = "against"
    out = []
    for h in settings.horizons_s:
        m = markouts(fills, mids, h)
        for name in ("all", "favours", "neutral", "against"):
            sel = m.notna() & (group == name if name != "all" else True)
            n = int(sel.sum())
            net = m[sel] - settings.maker_fee_bps
            se = net.std() / math.sqrt(n) if n > 1 else math.nan
            out.append(
                (
                    name,
                    h,
                    n,
                    n / hours if hours > 0 else math.nan,
                    at_fill[sel].mean() if n else math.nan,
                    m[sel].mean() if n else math.nan,
                    net.mean() if n else math.nan,
                    net.mean() / se if se and se > 0 else math.nan,
                )
            )
    return pd.DataFrame(out, columns=COLUMNS)


def study(catalog_path: str, instrument_id: str, settings: MakerSettings, start=None, end=None):
    from nautilus_trader.persistence import ParquetDataCatalog

    catalog = ParquetDataCatalog(catalog_path)
    deltas = catalog.query_order_book_deltas([instrument_id], start, end)
    trades = catalog.query_trade_ticks([instrument_id], start, end)
    fills, mids = simulate_fills(deltas, trades, settings)
    return fills, mids, evaluate(fills, mids, settings)
