"""
Order-book and trade-flow features for fast trading, ported from the Rust research crate on
`chatgpt/live-upgrade-market-data-20261027` (archived as tag archive/chatgpt/live-upgrade-market-data-20261027;
rust/crates/signals: ofi.rs, microprice.rs, vpin.rs, hawkes.rs, lead_lag.rs, absorption.rs) with the same formulas
and constants, and the departures listed per class. They are research features, not trading rules: none has a calibrated threshold, and
`lab micro-study` measures whether any of them predicts price moves larger than trading costs.

Books are lists of (price, size) from the best level outwards. Every register is fed in event-time order and must
be reset when the book is rebuilt from a snapshot or a feed gap is detected.

- MultiLevelOfi: 10-level order-flow imbalance (Cont, Kukanov & Stoikov 2014, "The Price Impact of Order Book
  Events"; multi-level weighting as in Xu, Gould & Howison 2018), decayed over 0.25 s and z-scored against a 30 s
  EWMA. Level-indexed: a change of rank is not an individual add or cancel.
- book_imbalance: 5-level weighted size imbalance and the microprice it implies (Stoikov 2018, "The micro-price").
- FlowToxicity: aggressor-signed volume buckets of fixed notional, averaged over the last 50 completed buckets.
  A VPIN-style proxy (Easley, Lopez de Prado & O'Hara 2012), not their bulk-classified estimator.
- HawkesIntensity: self-exciting intensity of aggressive buys and of aggressive sells (Hawkes 1971; Bacry, Mastromatteo
  & Muzy 2015 for its use on order flow), fixed exponential kernels, size-weighted. Not a fitted Hawkes model.
- KalmanLeadLag: a Kalman estimate of how much of a leader's return the laggard follows, updated only on overlapping
  return intervals (no forward fill), and the decayed gap the laggard has not yet closed.
- Absorption: aggressive volume per bps the touch gave way, both sides. A candidate absorption proxy: no L2 data can
  prove an iceberg or tell replenishment from spoofing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

OFI_LEVELS = 10
OFI_TAU_S = 0.250
OFI_NORMALIZE_TAU_S = 30.0
IMBALANCE_LEVELS = 5
FLOW_BUCKETS = 50


def level_weights(n: int) -> list[float]:
    """exp(-0.35 k): the best level counts fully, the tenth about 4%."""
    return [math.exp(-0.35 * k) for k in range(n)]


def _decay(dt_s: float, tau_s: float) -> float:
    return math.exp(-dt_s / tau_s)


def _valid(bids, asks, levels: int) -> bool:
    if len(bids) < levels or len(asks) < levels or bids[0][0] >= asks[0][0]:
        return False
    return all(p > 0 and q > 0 for p, q in bids[:levels]) and all(p > 0 and q > 0 for p, q in asks[:levels])


@dataclass(frozen=True)
class OfiSnapshot:
    ofi: float  # decayed, in base units (e.g. BTC)
    mean: float
    variance: float
    z: float  # positive = net buying pressure on the book


class MultiLevelOfi:
    def __init__(self):
        self.weights = level_weights(OFI_LEVELS)
        self.reset()

    def reset(self):
        self.prev_ns = None
        self.bids = self.asks = None
        self.ofi = self.mean = self.variance = 0.0

    def update(self, ts_ns: int, bids, asks) -> OfiSnapshot | None:
        """None on the first observation after a reset and while the book is thinner than 10 levels a side."""
        if not _valid(bids, asks, OFI_LEVELS):
            return None
        if self.prev_ns is not None and ts_ns < self.prev_ns:
            raise ValueError("book updates out of time order")
        bids, asks = list(bids[:OFI_LEVELS]), list(asks[:OFI_LEVELS])
        if self.prev_ns is None:
            self.prev_ns, self.bids, self.asks = ts_ns, bids, asks
            return None
        dt = (ts_ns - self.prev_ns) * 1e-9
        event = 0.0
        for w, (bp, bq), (pbp, pbq), (ap, aq), (pap, paq) in zip(
            self.weights, bids, self.bids, asks, self.asks, strict=True
        ):
            d_bid = bq if bp > pbp else (bq - pbq if bp == pbp else -pbq)
            d_ask = -paq if ap > pap else (aq - paq if ap == pap else aq)
            event += w * (d_bid - d_ask)
        ofi = self.ofi * _decay(dt, OFI_TAU_S) + event
        if self.variance == 0.0 and self.mean == 0.0 and self.ofi == 0.0:
            mean, variance = ofi, 0.0  # the first event initializes the EWMA without a false spike
        else:
            g = _decay(dt, OFI_NORMALIZE_TAU_S)
            r = ofi - self.mean
            mean, variance = self.mean + (1 - g) * r, g * (self.variance + (1 - g) * r * r)
        z = (ofi - mean) / math.sqrt(variance + 1e-9)
        self.prev_ns, self.bids, self.asks = ts_ns, bids, asks
        self.ofi, self.mean, self.variance = ofi, mean, variance
        return OfiSnapshot(ofi, mean, variance, z)


@dataclass(frozen=True)
class Imbalance:
    mid: float
    microprice: float
    imbalance: float  # in [-1, 1]; positive = more size bid than offered
    divergence_bps: float  # microprice over mid


def book_imbalance(bids, asks) -> Imbalance | None:
    if not _valid(bids, asks, IMBALANCE_LEVELS):
        return None
    w = level_weights(IMBALANCE_LEVELS)
    qb = sum(wk * q for wk, (_, q) in zip(w, bids, strict=False))
    qa = sum(wk * q for wk, (_, q) in zip(w, asks, strict=False))
    imbalance = (qb - qa) / (qb + qa)
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    micro = mid + imbalance * (ask - bid) / 2
    return Imbalance(mid, micro, imbalance, (micro / mid - 1) * 10_000)


@dataclass(frozen=True)
class Flow:
    toxicity: float  # mean |buy - sell| share of the last 50 buckets, in [0, 1]
    signed: float  # positive = buyer-initiated volume dominated; in [-1, 1]
    completed_buckets: int


class FlowToxicity:
    """Fixed-notional volume clock over aggressor-labelled trades. A trade larger than many buckets fills them
    all with its own sign (in constant time once it covers the whole window)."""

    def __init__(self, bucket_notional: float):
        if not bucket_notional > 0:
            raise ValueError("bucket notional must be positive")
        self.size = float(bucket_notional)
        self.reset()

    def reset(self):
        self.ring = [0.0] * FLOW_BUCKETS
        self.next = self.filled = self.total = 0
        self.partial_used = self.partial_signed = 0.0

    def _push(self, signed: float):
        self.ring[self.next] = signed
        self.next = (self.next + 1) % FLOW_BUCKETS
        self.filled = min(self.filled + 1, FLOW_BUCKETS)
        self.total += 1

    def observe(self, notional: float, buyer_initiated: bool) -> Flow | None:
        if not notional > 0:
            raise ValueError("trade notional must be positive")
        sign = 1.0 if buyer_initiated else -1.0
        first = self.size - self.partial_used
        if notional < first:
            self.partial_used += notional
            self.partial_signed += sign * notional
            return self.snapshot()
        self._push(self.partial_signed + sign * first)
        rest = notional - first
        full = int(rest // self.size)
        if full >= FLOW_BUCKETS:
            self.ring = [sign * self.size] * FLOW_BUCKETS
            self.next = (self.next + full) % FLOW_BUCKETS
            self.filled = FLOW_BUCKETS
            self.total += full
        else:
            for _ in range(full):
                self._push(sign * self.size)
        self.partial_used = rest - full * self.size
        self.partial_signed = sign * self.partial_used
        return self.snapshot()

    def snapshot(self) -> Flow | None:
        if self.filled < FLOW_BUCKETS:
            return None
        norm = FLOW_BUCKETS * self.size
        return Flow(sum(abs(x) for x in self.ring) / norm, sum(self.ring) / norm, self.total)


HAWKES_BETA_FAST = 8.0  # 1/s
HAWKES_ALPHA_FAST = 3.2
HAWKES_BETA_SLOW = 1.2
HAWKES_WEIGHT_CAP = 5.0  # a trade counts at most 5 average trades
HAWKES_MEAN_TAU_S = 300.0


@dataclass(frozen=True)
class HawkesSnapshot:
    isr_up: float  # intensity of aggressive buying over the baseline, >= 1
    isr_down: float
    up_acceleration: float  # fast minus slow channel: is the cascade speeding up?
    down_acceleration: float
    skew: float  # ln(isr_up / isr_down); positive = buy-side cascade


class HawkesIntensity:
    """Two one-way cascades (aggressive buys, aggressive sells), each a fast kernel (alpha 3.2, 1/8 s) and a slow one
    (alpha 0.8, 1/1.2 s). A trade excites its side by its notional over the 300 s running mean, capped at 5.

    Departures from hawkes.rs: no forced-liquidation channel (lab record does not record liquidations), and reading
    the intensity at a time (snapshot) decays a copy instead of moving the register, so sampling cannot change the
    mean-notional clock."""

    def __init__(self, baseline_per_s: float):
        if not baseline_per_s > 0:
            raise ValueError("baseline intensity must be positive")
        self.mu = float(baseline_per_s)
        self.reset()

    def reset(self):
        self.prev_ns = None
        self.fast = {True: 0.0, False: 0.0}  # keyed by buyer_initiated
        self.slow = {True: 0.0, False: 0.0}
        self.mean_notional = 0.0

    def observe(self, ts_ns: int, notional: float, buyer_initiated: bool) -> HawkesSnapshot:
        if not notional > 0:
            raise ValueError("trade notional must be positive")
        dt = self._elapsed(ts_ns)
        prev_mean = self.mean_notional or notional
        weight = min(notional / prev_mean, HAWKES_WEIGHT_CAP)
        for side in (True, False):
            self.fast[side] *= _decay(dt, 1 / HAWKES_BETA_FAST)
            self.slow[side] *= _decay(dt, 1 / HAWKES_BETA_SLOW)
        self.fast[buyer_initiated] += HAWKES_ALPHA_FAST * weight
        self.slow[buyer_initiated] += 0.25 * HAWKES_ALPHA_FAST * weight
        g = _decay(dt, HAWKES_MEAN_TAU_S)
        self.mean_notional = g * prev_mean + (1 - g) * notional
        self.prev_ns = ts_ns
        return self._snap(self.fast, self.slow)

    def snapshot(self, ts_ns: int) -> HawkesSnapshot | None:
        """The intensities at ts_ns, decayed since the last trade; None before the first trade."""
        if self.prev_ns is None:
            return None
        dt = self._elapsed(ts_ns)
        f, s = _decay(dt, 1 / HAWKES_BETA_FAST), _decay(dt, 1 / HAWKES_BETA_SLOW)
        return self._snap({k: v * f for k, v in self.fast.items()}, {k: v * s for k, v in self.slow.items()})

    def _elapsed(self, ts_ns: int) -> float:
        if self.prev_ns is None:
            return 0.0
        if ts_ns < self.prev_ns:
            raise ValueError("trades out of time order")
        return (ts_ns - self.prev_ns) * 1e-9

    def _snap(self, fast, slow) -> HawkesSnapshot:
        up, down = 1 + fast[True] / self.mu, 1 + fast[False] / self.mu
        return HawkesSnapshot(
            up, down, (fast[True] - slow[True]) / self.mu, (fast[False] - slow[False]) / self.mu, math.log(up / down)
        )


LEAD_LAG_MAX_INTERVAL_S = 0.050  # a longer gap between two prices is not a return interval
LEAD_LAG_SYNC_TAU_S = 0.050
LEAD_LAG_GAP_TAU_S = 1.0
LEAD_LAG_PRIOR = (1.0, 1.0)  # beta and its variance before any observation
LEAD_LAG_PROCESS_VAR = 1e-9
LEAD_LAG_OBSERVATION_VAR = 1e-7


@dataclass(frozen=True)
class LeadLagSnapshot:
    beta: float  # laggard log return per leader log return
    variance: float
    leader_return: float
    laggard_return: float
    lag_bps: float  # beta x leader return - laggard return: the part of the leader's move not yet followed
    sync_density: float  # share of recent laggard updates that found a fresh overlapping leader interval
    gap_bps: float  # lag_bps summed over recent matches, decayed over 1 s; positive = laggard should rise


class _Stream:
    def __init__(self):
        self.last_ns = None
        self.last_px = 0.0
        self.interval = None  # (start_ns, end_ns, log return)
        self.seq = 0

    def update(self, ts_ns: int, price: float, max_ns: int):
        if self.last_ns is not None and ts_ns < self.last_ns:
            raise ValueError("prices out of time order")
        if self.last_ns is None or ts_ns == self.last_ns or ts_ns - self.last_ns > max_ns:
            self.interval = None  # no elapsed time, or too long a gap to call a return
        else:
            self.interval = (self.last_ns, ts_ns, math.log(price / self.last_px))
        self.last_ns, self.last_px = ts_ns, price
        self.seq += 1


class KalmanLeadLag:
    """Scalar Kalman filter on laggard_return = beta x leader_return, updated when a new laggard return interval
    overlaps a leader interval not used before (an overlap-gated, O(1) relative of the Hayashi-Yoshida estimator,
    not the full sum). Observation noise grows as fewer laggard updates find such a pair. Joseph-form variance.

    Departures from lead_lag.rs: two prices of one stream at the same timestamp replace each other instead of being
    an error (a batch of book updates can share a receive time), and gap_bps accumulates lag_bps for sampling."""

    def __init__(self, max_interval_s: float = LEAD_LAG_MAX_INTERVAL_S):
        if not max_interval_s > 0:
            raise ValueError("max interval must be positive")
        self.max_ns = int(max_interval_s * 1e9)
        self.reset()

    def reset(self):
        self.leader, self.laggard = _Stream(), _Stream()
        self.last_ns = self.last_laggard_ns = None
        self.matched_seq = 0
        self.beta, self.variance = LEAD_LAG_PRIOR
        self.sync = 1.0
        self.gap, self.gap_ns = 0.0, None

    def observe(self, leader: bool, ts_ns: int, price: float) -> LeadLagSnapshot | None:
        """Feed both streams merged in time order. A snapshot only on a laggard update with a fresh overlap."""
        if not price > 0:
            raise ValueError("price must be positive")
        if self.last_ns is not None and ts_ns < self.last_ns:
            raise ValueError("prices out of time order")
        self.last_ns = ts_ns
        if leader:
            self.leader.update(ts_ns, price, self.max_ns)
            return None
        self.laggard.update(ts_ns, price, self.max_ns)
        dt = 0.0 if self.last_laggard_ns is None else (ts_ns - self.last_laggard_ns) * 1e-9
        self.last_laggard_ns = ts_ns
        x, y = self.leader.interval, self.laggard.interval
        matched = (
            x is not None and y is not None and x[0] < y[1] and y[0] < x[1] and self.leader.seq != self.matched_seq
        )
        a = _decay(dt, LEAD_LAG_SYNC_TAU_S)
        self.sync = min(max(a * self.sync + (1 - a) * matched, 0.05), 1.0)
        if not matched:
            return None
        xr, yr = x[2], y[2]
        r = LEAD_LAG_OBSERVATION_VAR / self.sync
        p = self.variance + LEAD_LAG_PROCESS_VAR
        gain = p * xr / (xr * xr * p + r)
        self.beta += gain * (yr - self.beta * xr)
        self.variance = (1 - gain * xr) ** 2 * p + gain * gain * r
        self.matched_seq = self.leader.seq
        lag_bps = (self.beta * xr - yr) * 10_000
        self.gap = self.gap_bps(ts_ns) + lag_bps
        self.gap_ns = ts_ns
        return LeadLagSnapshot(self.beta, self.variance, xr, yr, lag_bps, self.sync, self.gap)

    def gap_bps(self, ts_ns: int) -> float:
        """The decayed gap at ts_ns (0 before the first match)."""
        if self.gap_ns is None:
            return 0.0
        return self.gap * _decay((ts_ns - self.gap_ns) * 1e-9, LEAD_LAG_GAP_TAU_S)


ABSORPTION_WINDOW_S = 0.400
ABSORPTION_BASELINE_TAU_S = 300.0


@dataclass(frozen=True)
class AbsorptionSnapshot:
    bid_ratio: float  # recent aggressive selling, in units of its normal 400 ms volume, per bps the bid gave way
    ask_ratio: float  # the same for aggressive buying against the ask
    bid_displacement_bps: float  # how far the bid fell below its 400 ms reference
    ask_displacement_bps: float
    net: float  # bid_ratio - ask_ratio; positive = selling is being absorbed (support)


class _AbsorptionSide:
    def __init__(self):
        self.reference = 0.0
        self.volume = 0.0  # aggressive volume, decayed over 400 ms
        self.long = 0.0  # aggressive volume, decayed over 300 s: the baseline
        self.pending = 0.0  # traded since the last book update


class Absorption:
    """absorption.rs on both sides of the book: decayed aggressive volume against the touch, normalized by its
    usual 400 ms amount, divided by (displacement of the touch in bps + 0.5).

    Departures from absorption.rs: the mirror-image ask side, and the normal volume is computed here, past-only, as
    the 300 s decayed trade rate (bias-corrected while it warms up) x 400 ms, from trades before the current update."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.first_ns = self.prev_ns = None
        self.bid, self.ask = _AbsorptionSide(), _AbsorptionSide()

    def trade(self, qty: float, buyer_initiated: bool):
        if not qty > 0:
            raise ValueError("trade quantity must be positive")
        (self.ask if buyer_initiated else self.bid).pending += qty

    def update(self, ts_ns: int, best_bid: float, best_ask: float) -> AbsorptionSnapshot | None:
        """On a book update, after the trades received before it. None until both sides have a baseline."""
        if not 0 < best_bid < best_ask:
            raise ValueError("invalid touch")
        if self.prev_ns is not None and ts_ns < self.prev_ns:
            raise ValueError("book updates out of time order")
        if self.prev_ns is None:
            self.first_ns = ts_ns
            for side, px in ((self.bid, best_bid), (self.ask, best_ask)):
                side.reference, side.volume, side.long, side.pending = px, side.pending, side.pending, 0.0
            self.prev_ns = ts_ns
            return None
        dt = (ts_ns - self.prev_ns) * 1e-9
        age = (ts_ns - self.first_ns) * 1e-9
        g, gl = _decay(dt, ABSORPTION_WINDOW_S), _decay(dt, ABSORPTION_BASELINE_TAU_S)
        warm = 1 - _decay(age, ABSORPTION_BASELINE_TAU_S)
        out = []
        for side, px, sign in ((self.bid, best_bid, 1), (self.ask, best_ask, -1)):
            normal = side.long * gl / (ABSORPTION_BASELINE_TAU_S * warm) * ABSORPTION_WINDOW_S  # past trades only
            side.reference = g * side.reference + (1 - g) * px
            side.volume = side.volume * g + side.pending
            side.long = side.long * gl + side.pending
            side.pending = 0.0
            displacement = max(sign * (side.reference - px), 0.0) / px * 10_000
            out.append((side.volume / normal / (displacement + 0.5) if normal > 0 else None, displacement))
        self.prev_ns = ts_ns
        (bid_ratio, bid_disp), (ask_ratio, ask_disp) = out
        if bid_ratio is None or ask_ratio is None:
            return None
        return AbsorptionSnapshot(bid_ratio, ask_ratio, bid_disp, ask_disp, bid_ratio - ask_ratio)
