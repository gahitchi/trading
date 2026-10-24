"""
Statistics for judging backtests, following Bailey & López de Prado:

- psr():  Probabilistic Sharpe Ratio. Probability that the TRUE Sharpe ratio exceeds a
          benchmark, given the track length and the returns' skewness and fat tails.
          Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier".
- dsr():  Deflated Sharpe Ratio. The PSR against the Sharpe ratio you would expect from
          the BEST of N unskilled trials, so trying many strategies is paid for.
          Bailey & López de Prado (2014), "The Deflated Sharpe Ratio".
- pbo():  Probability of Backtest Overfitting via combinatorially symmetric
          cross-validation (CSCV): how often the in-sample winner ranks below the median
          out of sample. Bailey, Borwein, López de Prado & Zhu (2017), "The Probability
          of Backtest Overfitting".
- sharpe_ci(): block-bootstrap confidence interval for an annualized Sharpe ratio.

Sharpe ratios inside the formulas are per period (daily), not annualized.
"""

from __future__ import annotations

import itertools
import math
from statistics import NormalDist

import numpy as np

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()
DAYS = 252


def moments(r: np.ndarray) -> tuple[float, float, float, int]:
    """(daily Sharpe, skewness, kurtosis (normal = 3), number of observations)."""
    r = np.asarray(r, dtype=float)
    if not np.isfinite(r).all():
        raise ValueError("returns must be finite; refusing to discard invalid observations")
    t = len(r)
    if t < 3:
        return 0.0, 0.0, 3.0, t
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0, 0.0, 3.0, t
    z = (r - r.mean()) / r.std(ddof=0)
    return float(r.mean() / sd), float(np.mean(z**3)), float(np.mean(z**4)), t


def psr(sr: float, t: int, skew: float, kurt: float, sr_benchmark: float = 0.0) -> float:
    """P(true daily Sharpe > sr_benchmark)."""
    if t < 2:
        return float("nan")
    var = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if var <= 0:
        return float("nan")
    return _N.cdf((sr - sr_benchmark) * math.sqrt(t - 1) / math.sqrt(var))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum daily Sharpe among n_trials unskilled strategies whose Sharpe
    estimates have variance sr_variance (the 'SR0' of the Deflated Sharpe Ratio)."""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    n = n_trials
    return math.sqrt(sr_variance) * (
        (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n * math.e))
    )


def hac_sharpe_variance(returns: np.ndarray) -> tuple[float, int]:
    """Conservative HAC variance of the *daily Sharpe estimator*.

    Newey-West Bartlett weights on the joint sample mean and second central
    moment, then the delta method for mean / standard deviation. The variance
    never falls below the skew/kurtosis-adjusted iid estimate: negative sample
    serial correlation must not increase claimed research confidence.
    """
    r = np.asarray(returns, dtype=float)
    sr, skew, kurt, n = moments(r)
    if n < 3:
        return float("nan"), 0
    iid = (1 - skew * sr + (kurt - 1) / 4 * sr**2) / (n - 1)
    if not math.isfinite(iid) or iid <= 0:
        return float("nan"), 0
    center = r - r.mean()
    variance = float(np.mean(center**2))
    if variance <= 0:
        return iid, 0
    sigma = math.sqrt(variance)
    g = np.column_stack((center, center**2 - variance))
    gradient = np.array((1 / sigma, -float(r.mean()) / (2 * sigma**3)))
    lags = min(n - 1, max(1, int(math.floor(4 * (n / 100) ** (2 / 9)))))
    long_run = g.T @ g / n
    for lag in range(1, lags + 1):
        cross = g[lag:].T @ g[:-lag] / n
        long_run += (1 - lag / (lags + 1)) * (cross + cross.T)
    result = float(gradient @ long_run @ gradient / n)
    return max(iid, result) if math.isfinite(result) else float("nan"), lags


def dsr(returns: np.ndarray, trial_sharpes: list[float]) -> dict:
    """Deflated Sharpe Ratio of `returns`, given the daily Sharpe ratios of ALL trials
    run on the same data (including this one)."""
    sr, skew, kurt, t = moments(returns)
    n = max(1, len(trial_sharpes))
    var = float(np.var(trial_sharpes, ddof=1)) if len(trial_sharpes) > 1 else 0.0
    sr0 = expected_max_sharpe(n, var)
    sr_var, hac_lags = hac_sharpe_variance(returns)
    def probability(threshold: float) -> float:
        if math.isfinite(sr_var) and sr_var > 0:
            return _N.cdf((sr - threshold) / math.sqrt(sr_var))
        return float("nan")
    return {
        "sharpe_daily": sr,
        "sharpe_annual": sr * math.sqrt(DAYS),
        "skew": skew,
        "kurtosis": kurt,
        "observations": t,
        "trials": n,
        "expected_max_sharpe_annual": sr0 * math.sqrt(DAYS),
        "psr": probability(0.0),
        "dsr": probability(sr0),
        "sharpe_estimator_variance_hac": sr_var,
        "hac_lags": hac_lags,
    }


def sharpe_ci(
    returns: np.ndarray, *, level: float = 0.95, samples: int = 2000, block: int = 20, seed: int = 7
) -> tuple[float, float]:
    """Circular block-bootstrap confidence interval of the annualized Sharpe ratio."""
    r = np.asarray(returns, dtype=float)
    if not np.isfinite(r).all():
        raise ValueError("returns must be finite; refusing to discard invalid observations")
    n = len(r)
    if n < 2 * block:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    k = math.ceil(n / block)
    starts = rng.integers(0, n, size=(samples, k))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(samples, -1)[:, :n] % n
    boot = r[idx]
    sd = boot.std(axis=1, ddof=1)
    srs = np.where(sd > 0, boot.mean(axis=1) / np.where(sd > 0, sd, 1), 0.0) * math.sqrt(DAYS)
    lo, hi = np.quantile(srs, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def pbo(returns: np.ndarray, *, splits: int = 10) -> dict:
    """Probability of Backtest Overfitting by CSCV.

    returns: (T days x N configurations) matrix of daily returns, every configuration of
    one strategy family over the same days. The rows are cut into `splits` blocks; every
    way of choosing half the blocks as in-sample (the rest out-of-sample) is tried. For
    each, the configuration with the best in-sample Sharpe is located in the
    out-of-sample ranking. PBO = share of cases where it lands in the bottom half.
    """
    m = np.asarray(returns, dtype=float)
    if m.ndim != 2 or not np.isfinite(m).all():
        raise ValueError("PBO requires a finite two-dimensional returns matrix")
    t, n = m.shape
    if n < 2:
        raise ValueError("PBO needs at least 2 configurations")
    if splits % 2 or splits < 4:
        raise ValueError("splits must be an even number >= 4")
    if t < 2 * splits:
        raise ValueError("PBO needs at least two observations per block")
    blocks = np.array_split(np.arange(t), splits)

    def sharpe(x):
        sd = x.std(axis=0, ddof=1)
        return np.where(sd > 0, x.mean(axis=0) / np.where(sd > 0, sd, 1), 0.0)

    logits, degradation = [], []
    for combo in itertools.combinations(range(splits), splits // 2):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(splits) if i not in combo])
        sr_is, sr_oos = sharpe(m[is_idx]), sharpe(m[oos_idx])
        best = int(np.argmax(sr_is))
        rank = (sr_oos < sr_oos[best]).sum() + 0.5 * ((sr_oos == sr_oos[best]).sum() - 1) + 1  # 1..n
        omega = rank / (n + 1)
        logits.append(math.log(omega / (1 - omega)))
        degradation.append((sr_is[best] * math.sqrt(DAYS), sr_oos[best] * math.sqrt(DAYS)))
    logits = np.array(logits)
    deg = np.array(degradation)
    slope = float(np.polyfit(deg[:, 0], deg[:, 1], 1)[0]) if len(deg) > 2 and deg[:, 0].std() > 0 else float("nan")
    return {
        "pbo": float((logits <= 0).mean()),
        "combinations": len(logits),
        "configurations": n,
        "median_logit": float(np.median(logits)),
        "oos_sharpe_of_is_winner_mean": float(deg[:, 1].mean()),
        "is_sharpe_of_is_winner_mean": float(deg[:, 0].mean()),
        "degradation_slope": slope,  # OOS Sharpe per unit of IS Sharpe; ~1 = no overfitting, <=0 = pure noise
        "prob_oos_loss": float((deg[:, 1] < 0).mean()),
    }
