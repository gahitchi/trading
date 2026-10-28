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

from lab.micro import (
    FLOW_BUCKETS,
    HAWKES_ALPHA_FAST,
    Absorption,
    FlowToxicity,
    HawkesIntensity,
    KalmanLeadLag,
    MultiLevelOfi,
    book_imbalance,
    level_weights,
)
from lab.micro_study import FEATURES, StudySettings, evaluate, forward_returns_bps, sample_features

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


def test_hawkes_keeps_buy_and_sell_cascades_apart_and_decays_to_the_baseline():
    h = HawkesIntensity(1.0)
    assert h.snapshot(0) is None  # no trade yet
    buy = h.observe(0, 10.0, True)
    assert buy.isr_up == pytest.approx(1 + HAWKES_ALPHA_FAST) and buy.isr_down == 1.0 and buy.skew > 0
    assert buy.up_acceleration > 0  # the fast channel leads right after a burst
    sell = h.observe(0, 10.0, False)
    assert sell.isr_down == pytest.approx(sell.isr_up) and sell.skew == pytest.approx(0.0)
    later = h.snapshot(500 * MS)
    assert 1 < later.isr_up < buy.isr_up and later.up_acceleration < 0  # the slow channel outlasts the fast one
    assert h.snapshot(60_000 * MS).isr_up == pytest.approx(1.0)
    with pytest.raises(ValueError):
        h.observe(-1, 10.0, True)


def test_hawkes_weights_a_trade_by_its_size_capped_and_reading_it_changes_nothing():
    a, b = HawkesIntensity(1.0), HawkesIntensity(1.0)
    for h in (a, b):
        h.observe(0, 10.0, True)
    a.snapshot(100 * MS)  # sampling must not move the register
    assert a.observe(200 * MS, 1e6, False) == b.observe(200 * MS, 1e6, False)
    assert b.snapshot(200 * MS).isr_down == pytest.approx(1 + 5 * HAWKES_ALPHA_FAST)  # 1e5 x the mean, capped at 5


def test_lead_lag_learns_only_from_overlapping_fresh_intervals():
    k = KalmanLeadLag()
    assert k.observe(True, 1, 100.0) is None
    assert k.observe(False, 1 * MS, 50.0) is None  # no laggard interval yet
    assert k.observe(True, 10 * MS, 101.0) is None  # the leader alone never produces a snapshot
    s = k.observe(False, 15 * MS, 50.5)
    assert s is not None and s.leader_return == pytest.approx(math.log(1.01))
    assert k.observe(False, 20 * MS, 50.6) is None  # the same leader return is not used twice
    with pytest.raises(ValueError):
        k.observe(True, 19 * MS, 101.0)
    k.observe(True, 100 * MS, 102.0)  # 90 ms since the last leader price: too long to be a return interval
    assert k.observe(False, 101 * MS, 51.0) is None


def test_lead_lag_beta_converges_and_the_gap_points_to_the_unfollowed_move():
    rng = random.Random(3)
    k, x, y = KalmanLeadLag(), 100.0, 50.0
    for i in range(1, 3000):
        r = rng.gauss(0, 2e-4)
        x *= math.exp(r)
        y *= math.exp(0.5 * r + rng.gauss(0, 2e-5))
        k.observe(True, i * 20 * MS, x)
        k.observe(False, i * 20 * MS + MS, y)
    assert k.beta == pytest.approx(0.5, abs=0.03)
    ts = 3000 * 20 * MS
    k.observe(True, ts, x * 1.001)  # the leader jumps 10 bps, the laggard does not move
    snap = k.observe(False, ts + MS, y)
    assert snap.lag_bps == pytest.approx(5.0, abs=0.5) and snap.gap_bps > 4
    assert k.gap_bps(ts + 1001 * MS) == pytest.approx(snap.gap_bps / math.e, rel=1e-3)


def test_absorption_rises_when_selling_meets_a_bid_that_holds():
    a = Absorption()
    assert a.update(0, 99.9, 100.1) is None  # nothing to compare with yet
    for i in range(1, 600):  # 60 s of steady two-way flow sets the baseline
        a.trade(1.0, True)
        a.trade(1.0, False)
        a.update(i * 100 * MS, 99.9, 100.1)
    calm = a.update(600 * 100 * MS, 99.9, 100.1)
    a.trade(20.0, False)  # a burst of selling, bid unchanged
    held = a.update(601 * 100 * MS, 99.9, 100.1)
    assert held.bid_ratio > 5 * calm.bid_ratio and held.net > 0 and held.bid_displacement_bps == 0
    b = Absorption()
    for i in range(600):
        b.trade(1.0, True)
        b.trade(1.0, False)
        b.update(i * 100 * MS, 99.9, 100.1)
    b.update(600 * 100 * MS, 99.9, 100.1)
    b.trade(20.0, False)
    gave_way = b.update(601 * 100 * MS, 99.0, 99.2)  # the same burst, but the bid fell
    assert gave_way.bid_displacement_bps > 0 and gave_way.bid_ratio < held.bid_ratio
    with pytest.raises(ValueError):
        b.update(0, 99.0, 99.2)


def test_absorption_baseline_uses_only_trades_before_the_update():
    a, b = Absorption(), Absorption()
    for x in (a, b):
        x.update(0, 99.9, 100.1)
        x.trade(1.0, False)
        x.trade(1.0, True)
        x.update(100 * MS, 99.9, 100.1)
    a.trade(1.0, False)
    b.trade(100.0, False)  # a bigger trade now must raise the ratio, not the baseline it is measured against
    ra, rb = a.update(200 * MS, 99.9, 100.1).bid_ratio, b.update(200 * MS, 99.9, 100.1).bid_ratio
    assert rb / ra == pytest.approx((math.exp(-0.25) + 100) / (math.exp(-0.25) + 1))


def snapshot_deltas(iid, i, ts, mid, tick=0.01):
    bids, asks = book(mid, tick=tick)
    out = [OrderBookDelta.clear(iid, i * 100, ts, ts)]
    orders = [(OrderSide.BUY, p, q) for p, q in bids] + [(OrderSide.SELL, p, q) for p, q in asks]
    for k, (side, p, q) in enumerate(orders):
        order = BookOrder(side, Price(p, 3), Quantity(q, 3), 0)
        out.append(OrderBookDelta(iid, BookAction.ADD, order, 128 * (k == len(orders) - 1), i * 100 + k + 1, ts, ts))
    return out


LEADER = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")


def paired_market(steps=3000, seed=11):
    """A leader and a laggard updated every 20 ms (the laggard 1 ms later). The laggard is the leader plus a
    short-lived dislocation that reverts within ~0.2 s, so the gap it opens predicts its next move."""
    rng = random.Random(seed)
    lag, leader, trades, x, e = [], [], [], 0.0, 0.0
    for i in range(steps):
        ts = i * 20 * MS
        x += rng.gauss(0, 2e-4)
        e = 0.9 * e + rng.gauss(0, 2e-4)
        leader += snapshot_deltas(LEADER, i, ts, round(100_000 * math.exp(x), 2))
        lag += snapshot_deltas(IID, i, ts + MS, round(100_000 * math.exp(x + e), 2))
        side = AggressorSide.BUY if rng.random() < 0.5 else AggressorSide.SELL
        trades.append(TradeTick(IID, Price(100_000.0, 2), Quantity(0.01, 3), side, TradeId(f"t{i}"), ts, ts))
    return lag, trades, leader


def test_study_finds_a_planted_lead_lag_edge():
    lag, trades, leader = paired_market()
    settings = StudySettings(horizons_s=(0.5,), flow_bucket_notional=500.0)
    samples = sample_features(lag, trades, settings, leader)
    assert samples["lead_lag_bps"].notna().mean() > 0.95
    table = evaluate(samples, settings).set_index("feature")
    assert table.loc["lead_lag_bps", "ic"] > 0.2
    assert table.loc["lead_lag_bps", "top_bps"] > 0 > table.loc["lead_lag_bps", "bottom_bps"]
    assert sample_features(lag, trades, settings)["lead_lag_bps"].isna().all()  # no leader, no lead-lag


def test_features_at_a_time_depend_only_on_data_received_before_it():
    lag, trades, leader = paired_market(1500)
    settings = StudySettings(flow_bucket_notional=500.0)
    full = sample_features(lag, trades, settings, leader)
    cut = 700 * 20 * MS + MS
    lag_cut, trades_cut, leader_cut = ([e for e in xs if e.ts_init <= cut] for xs in (lag, trades, leader))
    early = sample_features(lag_cut, trades_cut, settings, leader_cut)
    assert len(early) < len(full)
    pd.testing.assert_frame_equal(early, full.iloc[: len(early)])
    for name in FEATURES:
        if name != "ofi_z":  # every update here is a fresh snapshot, so there is no order flow to measure
            assert full[name].notna().any(), name
