"""
Does an order-book or trade-flow feature predict price moves bigger than the cost of trading them?

Replays recorded order-book deltas and trades (lab record) in receive-time order (ts_init: what a live strategy
would have seen, so no look-ahead), rebuilds the book, updates the lab.micro features on every complete book
update and samples them on a fixed clock (default every 100 ms) so bursts don't dominate. For each horizon it
reports:

- ic: Spearman rank correlation between the feature and the forward mid-price return.
- top_bps / bottom_bps: mean forward return when the feature is in its top / bottom decile.
- cost_bps: a taker round trip, 2 x fee plus the median quoted spread. A feature can only pay for itself where the
  conditional move it points to exceeds this.

Descriptive research, not a strategy test: it places no orders and has no fills, queue position or latency.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lab.micro import OFI_LEVELS, FlowToxicity, MultiLevelOfi, book_imbalance

F_LAST = 128
FEATURES = ("ofi_z", "imbalance", "micro_bps", "flow_signed")


@dataclass(frozen=True)
class StudySettings:
    every_ms: int = 100
    horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    taker_fee_bps: float = 5.0  # Binance USD-M base tier, approximate; set your own
    flow_bucket_notional: float = 100_000.0  # quote currency (USDT) per volume bucket


def _levels(levels) -> list[tuple[float, float]]:
    return [(float(lv.price), lv.size()) for lv in levels]


def sample_features(deltas, trades, settings: StudySettings) -> pd.DataFrame:
    """One row per sampling tick: ts (ns), mid, spread_bps and the features (NaN until each one is warm)."""
    from nautilus_trader.model import AggressorSide, BookAction, BookType, OrderBook

    if not deltas:
        return pd.DataFrame(columns=["ts", "mid", "spread_bps", *FEATURES])
    book = OrderBook(deltas[0].instrument_id, BookType.L2_MBP)
    ofi, flow = MultiLevelOfi(), FlowToxicity(settings.flow_bucket_notional)
    step = settings.every_ms * 1_000_000
    next_tick = 0
    ofi_z = flow_signed = np.nan
    rows = []
    # Stable sorts keep a batch's deltas in their recorded order; at equal times trades go first.
    events = heapq.merge(
        ((d.ts_init, 1, d) for d in sorted(deltas, key=lambda d: d.ts_init)),
        ((t.ts_init, 0, t) for t in sorted(trades, key=lambda t: t.ts_init)),
        key=lambda e: (e[0], e[1]),
    )
    for ts, is_book, ev in events:
        if not is_book:
            f = flow.observe(float(ev.price) * float(ev.size), ev.aggressor_side == AggressorSide.BUY)
            flow_signed = f.signed if f else np.nan
            continue
        if ev.action == BookAction.CLEAR:
            ofi.reset()  # rebuilt from a snapshot: level-to-level changes across it are not order flow
            ofi_z = np.nan
        book.apply_delta(ev)
        if not ev.flags & F_LAST:
            continue  # mid-batch: the book is half updated
        bids, asks = _levels(book.bids(OFI_LEVELS)), _levels(book.asks(OFI_LEVELS))
        snap = ofi.update(ts, bids, asks)
        if snap is not None:
            ofi_z = snap.z
        if ts < next_tick:
            continue
        next_tick = ts - ts % step + step
        imb = book_imbalance(bids, asks)
        if imb is None:
            continue
        spread_bps = (asks[0][0] - bids[0][0]) / imb.mid * 10_000
        rows.append((ts, imb.mid, spread_bps, ofi_z, imb.imbalance, imb.divergence_bps, flow_signed))
    return pd.DataFrame(rows, columns=["ts", "mid", "spread_bps", *FEATURES])


def forward_returns_bps(samples: pd.DataFrame, horizon_s: float) -> pd.Series:
    """Mid return from each sample to the last sample at or before ts + horizon; NaN past the end of the data."""
    ts = samples["ts"].to_numpy()
    mid = samples["mid"].to_numpy()
    target = ts + int(horizon_s * 1e9)
    idx = np.searchsorted(ts, target, side="right") - 1
    fwd = (mid[idx] / mid - 1) * 10_000
    fwd[target > ts[-1]] = np.nan
    return pd.Series(fwd, index=samples.index)


def evaluate(samples: pd.DataFrame, settings: StudySettings) -> pd.DataFrame:
    if samples.empty:
        return pd.DataFrame(columns=["feature", "horizon_s", "n", "ic", "top_bps", "bottom_bps", "cost_bps"])
    cost = 2 * settings.taker_fee_bps + float(samples["spread_bps"].median())
    out = []
    for h in settings.horizons_s:
        fwd = forward_returns_bps(samples, h)
        for name in FEATURES:
            pair = pd.DataFrame({"x": samples[name], "y": fwd}).dropna()
            if len(pair) < 30 or pair["x"].nunique() < 10:
                out.append((name, h, len(pair), np.nan, np.nan, np.nan, cost))
                continue
            lo, hi = pair["x"].quantile([0.1, 0.9])
            out.append(
                (
                    name,
                    h,
                    len(pair),
                    pair["x"].rank().corr(pair["y"].rank()),  # Spearman, without scipy
                    pair.loc[pair["x"] >= hi, "y"].mean(),
                    pair.loc[pair["x"] <= lo, "y"].mean(),
                    cost,
                )
            )
    return pd.DataFrame(out, columns=["feature", "horizon_s", "n", "ic", "top_bps", "bottom_bps", "cost_bps"])


def study(catalog_path: str, instrument_id: str, settings: StudySettings, start=None, end=None):
    from nautilus_trader.persistence import ParquetDataCatalog

    catalog = ParquetDataCatalog(catalog_path)
    deltas = catalog.query_order_book_deltas([instrument_id], start, end)
    trades = catalog.query_trade_ticks([instrument_id], start, end)
    samples = sample_features(deltas, trades, settings)
    return samples, evaluate(samples, settings)
