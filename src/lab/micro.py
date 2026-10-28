"""
Order-book and trade-flow features for fast trading, ported from the Rust research crate on
`chatgpt/live-upgrade-market-data-20261027` (rust/crates/signals: ofi.rs, microprice.rs, vpin.rs) with the same
formulas and constants. They are research features, not trading rules: none has a calibrated threshold, and
`lab micro-study` measures whether any of them predicts price moves larger than trading costs.

Books are lists of (price, size) from the best level outwards. Every register is fed in event-time order and must
be reset when the book is rebuilt from a snapshot or a feed gap is detected.

- MultiLevelOfi: 10-level order-flow imbalance (Cont, Kukanov & Stoikov 2014, "The Price Impact of Order Book
  Events"; multi-level weighting as in Xu, Gould & Howison 2018), decayed over 0.25 s and z-scored against a 30 s
  EWMA. Level-indexed: a change of rank is not an individual add or cancel.
- book_imbalance: 5-level weighted size imbalance and the microprice it implies (Stoikov 2018, "The micro-price").
- FlowToxicity: aggressor-signed volume buckets of fixed notional, averaged over the last 50 completed buckets.
  A VPIN-style proxy (Easley, Lopez de Prado & O'Hara 2012), not their bulk-classified estimator.
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
