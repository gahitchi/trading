"""
Risk-based sharing of a strategy's capital among its stocks ([portfolio] weighting).

  equal        1/N per stock (the default, and the fallback)
  inverse_vol  each stock gets capital in proportion to 1 / its volatility: every position carries a similar
               amount of risk instead of a similar amount of money ("naive risk parity")
  hrp          Hierarchical Risk Parity (López de Prado, 2016): cluster the stocks by correlation, then split
               capital top-down between the clusters of the dendrogram by inverse variance. Correlated stocks
               share one risk budget instead of each taking a full one, and no covariance matrix is inverted,
               so it stays stable with few observations.

Point in time by construction: the inputs are the closes the allocator has already seen. A return counts only
where a stock has a price at both ends of the interval, and each pair of stocks is compared over the intervals
THEY share (Gemini audit F-R11): one stock with gaps or a recent listing no longer shrinks everyone's history.
A stock with fewer than `min_obs` returns of its own gets the median variance of the others and no
correlation; when no stock has `min_obs` returns, every method falls back to equal weights.

Estimation (F-R10): correlations are shrunk toward zero with the analytical intensity of Schäfer & Strimmer
(2005), the Ledoit-Wolf family's form that accepts a different sample size per pair, and the covariance is
projected onto the positive semi-definite matrices. HRP clusters with average linkage (UPGMA), which does not
chain the way single linkage does.

Implemented with numpy rather than skfolio: two well-defined methods do not justify adding scikit-learn and
cvxpy to a hash-locked Docker build. A different allocator (skfolio included) has to beat these through the
same protocol as any hypothesis.
"""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np

METHODS = ("equal", "inverse_vol", "hrp")
MIN_RISK_RETURNS = 20  # shared returns before a risk-based weighting replaces equal weights


def interval_returns(rows, keep=None) -> dict:
    """{(ts_a, ts_b): log return} between consecutive closes; `keep(ts_a, ts_b)` selects intervals."""
    return {(a[0], b[0]): math.log(b[1] / a[1]) for a, b in pairwise(rows) if keep is None or keep(a[0], b[0])}


def matched_returns(closes: dict[str, list[tuple[int, float]]], tickers: list[str]) -> np.ndarray | None:
    """Log returns (stocks x intervals) over the intervals every stock shares at both ends, or None."""
    per = {t: interval_returns(closes.get(t, [])) for t in tickers}
    if not per:
        return None
    shared = sorted(set.intersection(*(set(r) for r in per.values())))
    if not shared:
        return None
    return np.array([[per[t][k] for k in shared] for t in tickers])


def nearest_psd(cov: np.ndarray, floor: float = 1e-12) -> np.ndarray:
    """Eigenvalues clipped at `floor`: the closest positive semi-definite matrix in the Frobenius sense."""
    vals, vecs = np.linalg.eigh((cov + cov.T) / 2)
    return (vecs * np.maximum(vals, floor)) @ vecs.T


def covariance(per: dict[str, dict], tickers: list[str], min_obs: int, newcomer=np.median) -> np.ndarray | None:
    """Pairwise, shrunk, PSD covariance of the stocks' interval returns (`per`: ticker -> interval_returns).
    A stock without `min_obs` returns of its own gets `newcomer` of the known variances (the median for sharing
    capital; volatility targeting uses the maximum). None when no stock has `min_obs` returns."""
    n = len(tickers)
    var = np.full(n, np.nan)
    for i, t in enumerate(tickers):
        r = np.fromiter(per[t].values(), float)
        if len(r) >= min_obs:
            var[i] = r.var(ddof=1)
    known = var[np.isfinite(var) & (var > 0)]
    if not len(known):
        return None
    var = np.where(np.isfinite(var) & (var > 0), var, float(newcomer(known)))
    corr = np.eye(n)
    noise = signal = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            shared = per[tickers[i]].keys() & per[tickers[j]].keys()
            if len(shared) < min_obs:
                continue  # too little common history: uncorrelated
            x = np.array([per[tickers[i]][k] for k in shared])
            y = np.array([per[tickers[j]][k] for k in shared])
            if x.std() == 0 or y.std() == 0:
                continue
            r = float(np.corrcoef(x, y)[0, 1])
            corr[i, j] = corr[j, i] = r
            noise += (1 - r * r) ** 2 / (len(shared) - 1)  # estimated variance of the sample correlation
            signal += r * r
    shrink = min(1.0, noise / signal) if signal > 0 else 1.0
    corr = np.where(np.eye(n, dtype=bool), 1.0, corr * (1 - shrink))
    sd = np.sqrt(var)
    return nearest_psd(corr * np.outer(sd, sd))


def equal(n: int) -> np.ndarray:
    return np.full(n, 1.0 / n)


def inverse_vol(cov: np.ndarray) -> np.ndarray:
    vol = np.sqrt(np.diag(cov))
    if not np.all(np.isfinite(vol)) or np.any(vol <= 0):
        return equal(len(vol))
    w = 1.0 / vol
    return w / w.sum()


def _average_linkage_tree(dist: np.ndarray):
    """Average-linkage (UPGMA) dendrogram as nested pairs of leaf indices. Single linkage chains: one close
    pair can pull a whole group in, peeling one stock off against all the others at the root."""
    clusters = {i: (i, [i]) for i in range(len(dist))}  # key -> (tree, leaves)
    while len(clusters) > 1:
        keys = list(clusters)
        best, pair = math.inf, None
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                d = float(np.mean([dist[x, y] for x in clusters[a][1] for y in clusters[b][1]]))
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


def hrp(cov: np.ndarray) -> np.ndarray:
    n = cov.shape[0]
    if n == 1:
        return np.ones(1)
    std = np.sqrt(np.diag(cov))
    if not np.all(np.isfinite(cov)) or np.any(std <= 0):
        return equal(n)
    corr = np.clip(cov / np.outer(std, std), -1.0, 1.0)
    dist = np.sqrt(np.clip(0.5 * (1.0 - corr), 0.0, None))
    # Split capital top-down along the dendrogram's own clusters (not by halving the ordered list, the
    # original paper's shortcut, which can cut a cluster in two and hand its members separate budgets).
    w = np.ones(n)
    stack = [_average_linkage_tree(dist)]
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
    cov = covariance({t: interval_returns(closes.get(t, [])) for t in tickers}, tickers, min_obs)
    if cov is None:
        return dict(zip(tickers, equal(n), strict=True))
    w = inverse_vol(cov) if method == "inverse_vol" else hrp(cov)
    return dict(zip(tickers, (float(x) for x in w), strict=True))
