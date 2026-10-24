"""
Risk-based sharing of a strategy's capital among its stocks ([portfolio] weighting).

  equal        1/N per stock (the default, and the fallback)
  inverse_vol  each stock gets capital in proportion to 1 / its volatility: every position carries a similar
               amount of risk instead of a similar amount of money ("naive risk parity")
  hrp          Hierarchical Risk Parity (López de Prado, 2016): cluster the stocks by correlation, then split
               capital top-down between the clusters of the dendrogram by inverse variance. Correlated stocks share one risk budget instead of each taking a full one, and no
               covariance matrix is inverted, so it stays stable with few observations.

Point in time by construction: the inputs are the closes the allocator has already seen, and returns are
used only where every stock has a price at both ends of the same interval. With too little shared history
(fewer than `min_obs` returns) every method falls back to equal weights for the whole group.

Implemented with numpy rather than skfolio: two well-defined methods do not justify adding scikit-learn and
cvxpy to a hash-locked Docker build. A different allocator (skfolio included) has to beat these through the
same protocol as any hypothesis.
"""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np

METHODS = ("equal", "inverse_vol", "hrp")


def matched_returns(closes: dict[str, list[tuple[int, float]]], tickers: list[str]) -> np.ndarray | None:
    """Log returns (stocks x intervals) over the intervals every stock shares at both ends, or None."""
    per = {}
    for t in tickers:
        rows = closes.get(t, [])
        per[t] = {(a[0], b[0]): math.log(b[1] / a[1]) for a, b in pairwise(rows)}
    if not per:
        return None
    shared = sorted(set.intersection(*(set(r) for r in per.values())))
    if not shared:
        return None
    return np.array([[per[t][k] for k in shared] for t in tickers])


def equal(n: int) -> np.ndarray:
    return np.full(n, 1.0 / n)


def inverse_vol(rets: np.ndarray) -> np.ndarray:
    vol = rets.std(axis=1, ddof=1)
    if not np.all(np.isfinite(vol)) or np.any(vol <= 0):
        return equal(len(vol))
    w = 1.0 / vol
    return w / w.sum()


def _single_linkage_tree(dist: np.ndarray):
    """Single-linkage dendrogram as nested pairs of leaf indices."""
    clusters = {i: (i, [i]) for i in range(len(dist))}  # key -> (tree, leaves)
    while len(clusters) > 1:
        keys = list(clusters)
        best, pair = math.inf, None
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                d = min(dist[x, y] for x in clusters[a][1] for y in clusters[b][1])
                if d < best:
                    best, pair = d, (a, b)
        a, b = pair
        (ta, la), (tb, lb) = clusters[a], clusters.pop(b)
        clusters[a] = ((ta, tb), la + lb)
    return next(iter(clusters.values()))[0]


def _leaves(tree) -> list[int]:
    return [tree] if isinstance(tree, int) else _leaves(tree[0]) + _leaves(tree[1])


def _cluster_var(cov: np.ndarray, idx: list[int]) -> float:
    sub = cov[np.ix_(idx, idx)]
    ivp = 1.0 / np.diag(sub)
    ivp /= ivp.sum()
    return float(ivp @ sub @ ivp)


def hrp(rets: np.ndarray) -> np.ndarray:
    n = rets.shape[0]
    if n == 1:
        return np.ones(1)
    cov = np.atleast_2d(np.cov(rets))
    std = np.sqrt(np.diag(cov))
    if not np.all(np.isfinite(cov)) or np.any(std <= 0):
        return equal(n)
    corr = np.clip(cov / np.outer(std, std), -1.0, 1.0)
    dist = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, None))
    # Split capital top-down along the dendrogram's own clusters (not by halving the ordered list, the
    # original paper's shortcut, which can cut a cluster in two and hand its members separate budgets).
    w = np.ones(n)
    stack = [_single_linkage_tree(dist)]
    while stack:
        node = stack.pop()
        if isinstance(node, int):
            continue
        left, right = _leaves(node[0]), _leaves(node[1])
        v_left, v_right = _cluster_var(cov, left), _cluster_var(cov, right)
        alpha = 1.0 - v_left / (v_left + v_right)
        w[left] *= alpha
        w[right] *= 1.0 - alpha
        stack += [node[0], node[1]]
    return w / w.sum()


def risk_weights(method: str, closes: dict, tickers: list[str], min_obs: int) -> dict[str, float]:
    """Fractions of a group's capital per stock (summing to 1), point in time; equal without enough data."""
    if method not in METHODS:
        raise ValueError(f"unknown weighting {method!r}")
    n = len(tickers)
    if n == 0:
        return {}
    if method == "equal" or n == 1:
        return dict(zip(tickers, equal(n), strict=True))
    rets = matched_returns(closes, tickers)
    if rets is None or rets.shape[1] < min_obs:
        return dict(zip(tickers, equal(n), strict=True))
    w = inverse_vol(rets) if method == "inverse_vol" else hrp(rets)
    return dict(zip(tickers, (float(x) for x in w), strict=True))
