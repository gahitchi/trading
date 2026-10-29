"""
Does an order-book or trade-flow feature predict price moves bigger than the cost of trading them?

Replays recorded order-book deltas and trades (lab record) in receive-time order (ts_init: what a live strategy
would have seen, so no look-ahead), rebuilds the book (and, for lead-lag, a leader instrument's book: another
symbol or the same symbol on another venue), updates the lab.micro features on every complete book
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

from lab.micro import (
    OFI_LEVELS,
    Absorption,
    FlowToxicity,
    HawkesIntensity,
    KalmanLeadLag,
    MultiLevelOfi,
    book_imbalance,
)
from lab.study_time import MAX_BOOK_GAP_NS, future_mids

F_LAST = 128
FEATURES = ("ofi_z", "imbalance", "micro_bps", "flow_signed", "hawkes_skew", "absorption", "lead_lag_bps")
TRADE, LEADER_BOOK, BOOK = 0, 1, 2  # at equal receive times: trades, then the leader's book, then the book


@dataclass(frozen=True)
class StudySettings:
    every_ms: int = 100
    horizons_s: tuple[float, ...] = (1.0, 5.0, 30.0)
    taker_fee_bps: float = 5.0  # Binance USD-M base tier, approximate; set your own
    flow_bucket_notional: float = 100_000.0  # quote currency (USDT) per volume bucket
    hawkes_baseline_per_s: float = 1.0  # trades/s the Hawkes intensities are measured against
    lead_lag_max_interval_s: float = 0.050  # longest gap between two mids that still counts as a return interval


def _levels(levels) -> list[tuple[float, float]]:
    return [(float(lv.price), lv.size()) for lv in levels]


def sample_features(deltas, trades, settings: StudySettings, leader_deltas=()) -> pd.DataFrame:
    """One row per sampling tick: ts (ns), mid, spread_bps and the features (NaN until each one is warm;
    lead_lag_bps stays NaN without leader_deltas)."""
    from nautilus_trader.model import AggressorSide, BookAction, BookType, OrderBook

    if not deltas:
        return pd.DataFrame(columns=["ts", "mid", "spread_bps", *FEATURES, "segment"])
    book = OrderBook(deltas[0].instrument_id, BookType.L2_MBP)
    leader_book = OrderBook(leader_deltas[0].instrument_id, BookType.L2_MBP) if leader_deltas else None
    ofi, flow = MultiLevelOfi(), FlowToxicity(settings.flow_bucket_notional)
    hawkes, absorption = HawkesIntensity(settings.hawkes_baseline_per_s), Absorption()
    lead_lag = KalmanLeadLag(settings.lead_lag_max_interval_s)
    step = settings.every_ms * 1_000_000
    next_tick = 0
    ofi_z = flow_signed = absorption_net = np.nan
    last_book_ns = last_leader_ns = None
    segment = 0
    rows = []
    # Stable sorts keep a batch's deltas in their recorded order.
    events = heapq.merge(
        ((d.ts_init, BOOK, d) for d in sorted(deltas, key=lambda d: d.ts_init)),
        ((d.ts_init, LEADER_BOOK, d) for d in sorted(leader_deltas, key=lambda d: d.ts_init)),
        ((t.ts_init, TRADE, t) for t in sorted(trades, key=lambda t: t.ts_init)),
        key=lambda e: (e[0], e[1]),
    )
    for ts, kind, ev in events:
        if kind == TRADE:
            if last_book_ns is None or ts - last_book_ns > MAX_BOOK_GAP_NS:
                continue  # no fresh book against which this trade could be observed
            buy = ev.aggressor_side == AggressorSide.BUY
            f = flow.observe(float(ev.price) * float(ev.size), buy)
            flow_signed = f.signed if f else np.nan
            hawkes.observe(ts, float(ev.price) * float(ev.size), buy)
            absorption.trade(float(ev.size), buy)
            continue
        if kind == LEADER_BOOK:
            if last_leader_ns is not None and ts - last_leader_ns > MAX_BOOK_GAP_NS:
                leader_book = OrderBook(ev.instrument_id, BookType.L2_MBP)
                lead_lag.reset()
                last_leader_ns = None
            leader_book.apply_delta(ev)
            if ev.flags & F_LAST:
                mid = _mid(leader_book)
                if mid is not None:
                    last_leader_ns = ts
                    lead_lag.observe(True, ts, mid)
            continue
        if last_book_ns is not None and ts - last_book_ns > MAX_BOOK_GAP_NS:
            book = OrderBook(ev.instrument_id, BookType.L2_MBP)
            ofi, flow = MultiLevelOfi(), FlowToxicity(settings.flow_bucket_notional)
            hawkes, absorption = HawkesIntensity(settings.hawkes_baseline_per_s), Absorption()
            lead_lag.reset()
            ofi_z = flow_signed = absorption_net = np.nan
            last_book_ns = None
            segment += 1
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
        if bids and asks and bids[0][0] < asks[0][0]:
            last_book_ns = ts
            a = absorption.update(ts, bids[0][0], asks[0][0])
            absorption_net = a.net if a else np.nan
            if leader_book is not None:
                lead_lag.observe(False, ts, (bids[0][0] + asks[0][0]) / 2)
        if ts < next_tick:
            continue
        next_tick = ts - ts % step + step
        imb = book_imbalance(bids, asks)
        if imb is None:
            continue
        spread_bps = (asks[0][0] - bids[0][0]) / imb.mid * 10_000
        h = hawkes.snapshot(ts)
        leader_fresh = last_leader_ns is not None and ts - last_leader_ns <= MAX_BOOK_GAP_NS
        lag = lead_lag.gap_bps(ts) if leader_fresh and lead_lag.gap_ns is not None else np.nan
        rows.append(
            (
                ts,
                imb.mid,
                spread_bps,
                ofi_z,
                imb.imbalance,
                imb.divergence_bps,
                flow_signed,
                h.skew if h else np.nan,
                absorption_net,
                lag,
                segment,
            )
        )
    return pd.DataFrame(rows, columns=["ts", "mid", "spread_bps", *FEATURES, "segment"])


def _mid(book) -> float | None:
    bids, asks = _levels(book.bids(1)), _levels(book.asks(1))
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return None
    return (bids[0][0] + asks[0][0]) / 2


def forward_returns_bps(samples: pd.DataFrame, horizon_s: float) -> pd.Series:
    """Mid return within one recorded segment; NaN inside a hole or past the end of the data."""
    ts = samples["ts"].to_numpy()
    mid = samples["mid"].to_numpy()
    future = future_mids(ts, mid, ts, horizon_s, samples.get("segment"))
    fwd = (future / mid - 1) * 10_000
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


def study(
    catalog_path: str,
    instrument_id: str,
    settings: StudySettings,
    start=None,
    end=None,
    leader_id=None,
    *,
    evidence=None,
):
    from nautilus_trader.persistence import ParquetDataCatalog

    catalog = ParquetDataCatalog(catalog_path)
    deltas = catalog.query_order_book_deltas([instrument_id], start, end)
    trades = catalog.query_trade_ticks([instrument_id], start, end)
    leader = catalog.query_order_book_deltas([leader_id], start, end) if leader_id else ()
    if evidence is not None:
        evidence.objects("book", deltas)
        evidence.objects("trades", trades)
        evidence.objects("leader", leader)
    samples = sample_features(deltas, trades, settings, leader)
    return samples, evaluate(samples, settings)
