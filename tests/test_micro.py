"""Fast-trading features (ported from the Rust research crate) and the study that tests them against costs."""

import math
import random

import numpy as np
import pandas as pd
import pytest
from nautilus_trader.model import (
    AggressorSide,
    BookAction,
    BookOrder,
    InstrumentId,
    OrderBookDelta,
    OrderSide,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)

from lab.micro import FLOW_BUCKETS, FlowToxicity, MultiLevelOfi, book_imbalance, level_weights
from lab.micro_study import StudySettings, evaluate, forward_returns_bps, sample_features

IID = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
MS = 1_000_000


def book(mid=100.0, bid_sizes=None, ask_sizes=None, tick=0.1):
    bid_sizes = bid_sizes or [1.0] * 10
    ask_sizes = ask_sizes or [1.0] * 10
    bids = [(round(mid - tick / 2 - k * tick, 2), q) for k, q in enumerate(bid_sizes)]
    asks = [(round(mid + tick / 2 + k * tick, 2), q) for k, q in enumerate(ask_sizes)]
    return bids, asks


def test_ofi_counts_resting_size_changes_with_their_side_and_level_weight():
    ofi = MultiLevelOfi()
    assert ofi.update(0, *book()) is None  # first observation only sets the reference
    bids, asks = book(bid_sizes=[3.0] + [1.0] * 9)
    first = ofi.update(100 * MS, bids, asks)
    assert first.ofi == pytest.approx(2.0)  # +2 at the best bid, weight 1
    assert first.z == 0.0  # the first event initializes the normalization: no false spike
    bids, asks = book(bid_sizes=[3.0] + [1.0] * 9, ask_sizes=[1.0, 4.0] + [1.0] * 8)
    second = ofi.update(200 * MS, bids, asks)
    assert second.ofi == pytest.approx(2.0 * math.exp(-0.1 / 0.25) - 3.0 * level_weights(2)[1])
    assert second.z < 0  # added offers are selling pressure


def test_ofi_treats_a_better_bid_as_new_size_and_rejects_time_reversal():
    ofi = MultiLevelOfi()
    bids, asks = book()
    ofi.update(0, bids, asks)
    improved = [(bids[0][0] + 0.05, 2.0), *bids[:9]]
    assert ofi.update(MS, improved, asks).ofi > 0
    with pytest.raises(ValueError):
        ofi.update(0, improved, asks)
    assert ofi.update(2 * MS, bids[:5], asks) is None  # too thin to compare ten levels


def test_imbalance_moves_the_microprice_towards_the_thin_side():
    even = book_imbalance(*book())
    assert even.imbalance == 0 and even.microprice == even.mid
    heavy_bid = book_imbalance(*book(bid_sizes=[9.0] * 10))
    assert heavy_bid.imbalance == pytest.approx(0.8)
    assert heavy_bid.microprice > heavy_bid.mid and heavy_bid.divergence_bps > 0
    assert book_imbalance([(100.0, 1.0)] * 5, [(99.0, 1.0)] * 5) is None  # crossed


def test_flow_needs_fifty_buckets_and_signs_buyer_initiated_volume_positive():
    flow = FlowToxicity(1_000.0)
    for _ in range(FLOW_BUCKETS - 1):
        assert flow.observe(1_000.0, True) is None
    full = flow.observe(1_000.0, True)
    assert (full.signed, full.toxicity, full.completed_buckets) == (1.0, 1.0, FLOW_BUCKETS)
    for _ in range(FLOW_BUCKETS):
        flow.observe(500.0, True)
        flow.observe(500.0, False)  # each bucket balanced
    assert flow.snapshot().toxicity == pytest.approx(0.0)


def test_one_huge_trade_fills_the_window_in_constant_time():
    flow = FlowToxicity(1.0)
    snap = flow.observe(1e12, False)
    assert (snap.signed, snap.toxicity) == (-1.0, 1.0)
    assert snap.completed_buckets == 10**12


def test_forward_returns_use_the_last_sample_inside_the_horizon():
    s = pd.DataFrame({"ts": [0, 400 * MS, 1000 * MS, 1500 * MS], "mid": [100.0, 101.0, 102.0, 103.0]})
    fwd = forward_returns_bps(s, 1.0)
    assert fwd[0] == pytest.approx(200.0)  # the 1.0 s sample, not the 1.5 s one
    assert fwd[1] == pytest.approx((102 / 101 - 1) * 10_000)  # 1.4 s target: the 1.0 s sample
    assert np.isnan(fwd[2]) and np.isnan(fwd[3])  # horizon runs past the data


def synthetic_market(steps=3000, seed=7):
    """Every 100 ms a fresh book whose size imbalance predicts the next second's move; trades are noise."""
    rng = random.Random(seed)
    deltas, trades, mid, pending = [], [], 1000.0, []
    for i in range(steps):
        ts = i * 100 * MS
        tilt = rng.uniform(-1, 1)
        pending.append(tilt)
        if len(pending) > 10:  # the tilt seen 1 s ago moves the price now
            mid += pending.pop(0) * 0.2
        mid = round(mid, 1)
        bids, asks = book(mid + 0.05, [1 + max(tilt, 0) * 8] * 10, [1 + max(-tilt, 0) * 8] * 10)
        deltas.append(OrderBookDelta.clear(IID, i * 100, ts, ts))
        orders = [(OrderSide.BUY, p, q) for p, q in bids] + [(OrderSide.SELL, p, q) for p, q in asks]
        for k, (side, p, q) in enumerate(orders):
            flags = 128 if k == len(orders) - 1 else 0
            order = BookOrder(side, Price(p, 2), Quantity(q, 3), 0)
            deltas.append(OrderBookDelta(IID, BookAction.ADD, order, flags, i * 100 + k + 1, ts, ts))
        side = AggressorSide.BUY if rng.random() < 0.5 else AggressorSide.SELL
        trades.append(TradeTick(IID, Price(mid, 2), Quantity(0.5, 3), side, TradeId(f"t{i}"), ts, ts))
    return deltas, trades


def test_study_finds_a_planted_edge_and_charges_trading_costs():
    settings = StudySettings(horizons_s=(1.0,), taker_fee_bps=5.0, flow_bucket_notional=2_000.0)
    deltas, trades = synthetic_market()
    samples = sample_features(deltas, trades, settings)
    assert len(samples) == 3000
    assert samples["ofi_z"].isna().all()  # every step is a fresh snapshot: no order flow to measure
    table = evaluate(samples, settings).set_index("feature")
    assert table.loc["imbalance", "ic"] > 0.2  # one of the ten tilts behind each 1 s move: about 1 / sqrt(10)
    assert table.loc["imbalance", "top_bps"] > 0 > table.loc["imbalance", "bottom_bps"]
    assert abs(table.loc["flow_signed", "ic"]) < 0.1  # random trade signs carry nothing
    assert table.loc["imbalance", "cost_bps"] == pytest.approx(10.0 + 0.1 / 1000 * 10_000, rel=0.05)


def test_study_tracks_order_flow_across_incremental_updates_and_resets_on_a_snapshot():
    bids, asks = book()
    orders = [(OrderSide.BUY, p, q) for p, q in bids] + [(OrderSide.SELL, p, q) for p, q in asks]
    deltas = [
        OrderBookDelta(IID, BookAction.ADD, BookOrder(s, Price(p, 2), Quantity(q, 3), 0), 128 * (k == 19), k, 0, 0)
        for k, (s, p, q) in enumerate(orders)
    ]
    for i in range(1, 4):  # the best bid grows every 100 ms
        ts = i * 100 * MS
        order = BookOrder(OrderSide.BUY, Price(bids[0][0], 2), Quantity(1 + i, 3), 0)
        deltas.append(OrderBookDelta(IID, BookAction.UPDATE, order, 128, 100 + i, ts, ts))
    deltas.append(OrderBookDelta.clear(IID, 200, 400 * MS, 400 * MS))
    for k, (s, p, q) in enumerate(orders):
        order = BookOrder(s, Price(p, 2), Quantity(q, 3), 0)
        deltas.append(OrderBookDelta(IID, BookAction.ADD, order, 128 * (k == 19), 201 + k, 400 * MS, 400 * MS))
    samples = sample_features(deltas, [], StudySettings()).set_index("ts")
    assert np.isnan(samples.loc[0, "ofi_z"])  # nothing to compare with yet
    assert samples.loc[300 * MS, "ofi_z"] > 0  # sustained bid growth is buying pressure
    assert np.isnan(samples.loc[400 * MS, "ofi_z"])  # a rebuilt book restarts the register
