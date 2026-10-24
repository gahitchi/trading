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
- sharpe_ci(): studentized stationary-bootstrap confidence interval for an annualized Sharpe ratio.

Sharpe ratios inside the formulas are per period (daily), not annualized.

Serial correlation (Gemini audit F-R01): the variance of a Sharpe estimate is computed with the delta method
on the (mean, variance) moment pair and a Newey-West (Bartlett) long-run covariance (Lo, 2002; Mertens, 2002).
Under i.i.d. returns it reduces exactly to Bailey & López de Prado's skew/kurtosis formula; the larger of the
two is used, so autocorrelation can widen an interval but a noisy HAC estimate never narrows it.
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


def psr(
    sr: float, t: int, skew: float, kurt: float, sr_benchmark: float = 0.0, *, variance: float | None = None
) -> float:
    """P(true daily Sharpe > sr_benchmark). `variance` is the asymptotic variance of sqrt(T) x the Sharpe
    estimate (sharpe_variance()); without it, the i.i.d. skew/kurtosis formula is used."""
    if t < 2:
        return float("nan")
    var = 1 - skew * sr + (kurt - 1) / 4 * sr**2 if variance is None else variance
    if not var > 0:
        return float("nan")
    return _N.cdf((sr - sr_benchmark) * math.sqrt(t - 1) / math.sqrt(var))


def hac_lags(t: int) -> int:
    """Newey-West automatic bandwidth floor(4 (T/100)^(2/9))."""
    return int(4 * (t / 100) ** (2 / 9)) if t > 0 else 0


def sharpe_variance(r: np.ndarray, lags: int | None = None) -> float:
    """Asymptotic variance of sqrt(T) x (Sharpe estimate - Sharpe), robust to serial correlation (F-R01).

    Delta method on g_t = (r_t - mu, (r_t - mu)^2 - sigma^2) with a Bartlett-weighted Newey-West long-run
    covariance. Floored at the i.i.d. non-normal value, so positive autocorrelation widens the interval and a
    noisy negative estimate cannot narrow it."""
    r = np.asarray(r, dtype=float)
    t = len(r)
    if t < 3:
        return float("nan")
    mu, var = r.mean(), r.var()
    if var <= 0:
        return float("nan")
    sd = math.sqrt(var)
    g = np.column_stack([r - mu, (r - mu) ** 2 - var])
    s = g.T @ g / t
    lags = min(hac_lags(t) if lags is None else lags, t - 1)
    for lag in range(1, lags + 1):
        gamma = g[lag:].T @ g[:-lag] / t
        s += (1 - lag / (lags + 1)) * (gamma + gamma.T)
    grad = np.array([1 / sd, -mu / (2 * sd**3)])
    hac = float(grad @ s @ grad)
    z = (r - mu) / sd
    sr, skew, kurt = mu / sd, float(np.mean(z**3)), float(np.mean(z**4))
    iid = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    return max(hac, iid)


def expected_max_sharpe(n_trials: float, sr_variance: float) -> float:
    """Expected maximum daily Sharpe among n_trials unskilled strategies whose Sharpe
    estimates have variance sr_variance (the 'SR0' of the Deflated Sharpe Ratio).

    One trial has nothing to be the maximum of: SR0 = 0 and the DSR equals the PSR, by definition. n_trials
    may be fractional (an effective number of independent trials)."""
    if n_trials <= 1 or sr_variance <= 0:
        return 0.0
    n = n_trials
    value = math.sqrt(sr_variance) * (
        (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n * math.e))
    )
    return max(0.0, value)


def robust_variance(values: list[float], clip: float = 3.0) -> float:
    """Variance of the trials' Sharpe ratios, winsorized at median +/- clip robust standard deviations
    (1.4826 x MAD) so one degenerate trial cannot inflate SR0 (F-R02). For well-behaved trials it equals
    the sample variance."""
    x = np.asarray(values, dtype=float)
    if len(x) < 2:
        return 0.0
    if len(x) >= 3:
        med = float(np.median(x))
        scale = 1.4826 * float(np.median(np.abs(x - med)))
        if scale > 0:
            x = np.clip(x, med - clip * scale, med + clip * scale)
    return float(np.var(x, ddof=1))


def effective_trials(n: int, mean_correlation: float) -> float:
    """N_hat = 1 + (N - 1)(1 - rho_bar) (López de Prado, 2019). rho_bar is floored at 0: negatively
    correlated trials never count as more than N."""
    if n <= 1:
        return float(max(n, 1))
    rho = min(1.0, max(0.0, mean_correlation))
    return 1 + (n - 1) * (1 - rho)


def dsr(returns: np.ndarray, trial_sharpes: list[float], *, n_effective: float | None = None) -> dict:
    """Deflated Sharpe Ratio of `returns`, given the daily Sharpe ratios of ALL trials
    run on the same data (including this one). `n_effective` (<= the number of trials) discounts trials whose
    returns are correlated; the variance of the unskilled Sharpe ratios is winsorized."""
    sr, skew, kurt, t = moments(returns)
    n = max(1, len(trial_sharpes))
    n_eff = n if n_effective is None else min(float(n), max(1.0, float(n_effective)))
    sr0 = expected_max_sharpe(n_eff, robust_variance(trial_sharpes))
    variance = sharpe_variance(returns) if t >= 3 and sr != 0 else None
    return {
        "sharpe_daily": sr,
        "sharpe_annual": sr * math.sqrt(DAYS),
        "skew": skew,
        "kurtosis": kurt,
        "observations": t,
        "trials": n,
        "effective_trials": n_eff,
        "expected_max_sharpe_annual": sr0 * math.sqrt(DAYS),
        "psr": psr(sr, t, skew, kurt, 0.0, variance=variance),
        "dsr": psr(sr, t, skew, kurt, sr0, variance=variance),
    }


def optimal_block_length(x: np.ndarray) -> float:
    """Mean block length for the stationary bootstrap: Politis & White (2004) with the Patton, Politis &
    White (2009) correction, flat-top lag window. At least 1, at most min(3 sqrt(n), n / 3)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 10:
        return 1.0
    x = x - x.mean()
    kn = max(5, math.ceil(math.sqrt(math.log10(n))))
    mmax = math.ceil(math.sqrt(n)) + kn
    bmax = math.ceil(min(3 * math.sqrt(n), n / 3))
    acov = np.array([x[: n - k] @ x[k:] / n for k in range(mmax + 1)])
    if acov[0] <= 0:
        return 1.0
    rho = acov / acov[0]
    threshold = 2 * math.sqrt(math.log10(n) / n)
    m_hat = mmax - kn
    for m in range(mmax - kn + 1):
        if np.all(np.abs(rho[m + 1 : m + kn + 1]) < threshold):
            m_hat = m
            break
    big_m = min(2 * m_hat, mmax)
    k = np.arange(-big_m, big_m + 1)
    u = np.abs(k) / big_m if big_m else np.zeros(1)
    lam = np.where(u <= 0.5, 1.0, np.where(u <= 1, 2 * (1 - u), 0.0))
    r = acov[np.abs(k)]
    g = float(np.sum(lam * np.abs(k) * r))
    d = 2 * float(np.sum(lam * r)) ** 2
    if d <= 0 or g == 0:
        return 1.0
    return float(min(max((2 * g**2 / d) ** (1 / 3) * n ** (1 / 3), 1.0), bmax))


def block_length(r: np.ndarray) -> float:
    """Mean block length for return series: the longer of the dependence in returns and in their size
    (volatility clustering), so resamples keep both."""
    r = np.asarray(r, dtype=float)
    return max(optimal_block_length(r), optimal_block_length(np.abs(r)))


def stationary_indices(n: int, mean_block: float, samples: int, rng: np.random.Generator) -> np.ndarray:
    """Politis & Romano (1994) stationary bootstrap: blocks of geometric length (mean `mean_block`). A block
    that reaches the end of the series restarts at a random point instead of wrapping to the start (F-R04), so
    the last date is never stitched onto the first."""
    p = 1.0 / max(1.0, mean_block)
    idx = np.empty((samples, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, size=samples)
    restart = rng.random((samples, n)) < p
    fresh = rng.integers(0, n, size=(samples, n))
    for i in range(1, n):
        nxt = idx[:, i - 1] + 1
        idx[:, i] = np.where(restart[:, i] | (nxt >= n), fresh[:, i], nxt)
    return idx


def _sharpe_and_se(x: np.ndarray, lags: int) -> tuple[np.ndarray, np.ndarray]:
    """Row-wise daily Sharpe and its HAC standard error (sharpe_variance, vectorized)."""
    t = x.shape[1]
    mu = x.mean(axis=1, keepdims=True)
    var = x.var(axis=1, keepdims=True)
    sd = np.sqrt(np.where(var > 0, var, np.nan))
    e = x - mu
    g1, g2 = e, e**2 - var
    s11, s12, s22 = (g1 * g1).mean(1), (g1 * g2).mean(1), (g2 * g2).mean(1)
    for lag in range(1, lags + 1):
        w = 1 - lag / (lags + 1)
        a1, a2, b1, b2 = g1[:, lag:], g2[:, lag:], g1[:, :-lag], g2[:, :-lag]
        s11 = s11 + w * 2 * (a1 * b1).sum(1) / t
        s12 = s12 + w * ((a1 * b2).sum(1) + (a2 * b1).sum(1)) / t
        s22 = s22 + w * 2 * (a2 * b2).sum(1) / t
    sd1 = sd[:, 0]
    d1, d2 = 1 / sd1, -mu[:, 0] / (2 * sd1**3)
    hac = d1 * d1 * s11 + 2 * d1 * d2 * s12 + d2 * d2 * s22
    z = e / sd
    sr = mu[:, 0] / sd1
    iid = 1 - (z**3).mean(1) * sr + ((z**4).mean(1) - 1) / 4 * sr**2
    return sr, np.sqrt(np.maximum(hac, iid) / (t - 1))


def sharpe_ci(
    returns: np.ndarray,
    *,
    level: float = 0.95,
    samples: int = 2000,
    block: float | None = None,
    seed: int = 7,
) -> tuple[float, float]:
    """Studentized stationary-bootstrap confidence interval of the annualized Sharpe ratio (F-R04).

    Each resample's Sharpe is centred on the observed one and divided by its own HAC standard error; the
    quantiles of that t-statistic, scaled by the observed standard error, give the interval. Block lengths are
    geometric with the Politis-White mean (or `block`)."""
    r = np.asarray(returns, dtype=float)
    if not np.isfinite(r).all():
        raise ValueError("returns must be finite; refusing to discard invalid observations")
    n = len(r)
    if n < 40 or r.std() == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    mean_block = block_length(r) if block is None else float(block)
    lags = hac_lags(n)
    sr_hat, se_hat = _sharpe_and_se(r[None, :], lags)
    boot = r[stationary_indices(n, mean_block, samples, rng)]
    sr_b, se_b = _sharpe_and_se(boot, lags)
    ok = np.isfinite(se_b) & (se_b > 0)
    tstat = (sr_b[ok] - sr_hat[0]) / se_b[ok]
    q_lo, q_hi = np.quantile(tstat, [(1 - level) / 2, 1 - (1 - level) / 2])
    scale = math.sqrt(DAYS)
    return float((sr_hat[0] - q_hi * se_hat[0]) * scale), float((sr_hat[0] - q_lo * se_hat[0]) * scale)


def pbo(returns: np.ndarray, *, splits: int = 10, purge: int = 0, embargo: float = 0.0) -> dict:
    """Probability of Backtest Overfitting by CSCV, purged and embargoed (F-R03).

    returns: (T days x N configurations) matrix of daily returns, every configuration of
    one strategy family over the same days. The rows are cut into `splits` blocks; every
    way of choosing half the blocks as in-sample (the rest out-of-sample) is tried. For
    each, the configuration with the best in-sample Sharpe is located in the
    out-of-sample ranking. PBO = share of cases where it lands in the bottom half.

    Out-of-sample observations within `purge` bars of an in-sample block, on either side, are dropped, and so
    are ceil(T x embargo) more after each in-sample block, so serial dependence across a boundary cannot carry
    in-sample information into the out-of-sample ranking (López de Prado, 2018).
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
    if type(purge) is not int or purge < 0 or not 0 <= embargo < 0.5:
        raise ValueError("purge must be a nonnegative integer and embargo a fraction below 0.5")
    blocks = np.array_split(np.arange(t), splits)
    after = purge + math.ceil(t * embargo)

    def out_of_sample(combo) -> np.ndarray:
        keep = []
        for i in range(splits):
            if i in combo:
                continue
            b = blocks[i]
            lo = after if i > 0 and i - 1 in combo else 0
            hi = purge if i < splits - 1 and i + 1 in combo else 0
            kept = b[lo : len(b) - hi]
            if len(kept) < 2:
                raise ValueError(f"purge {purge} and embargo {embargo} leave fewer than 2 observations in a block")
            keep.append(kept)
        return np.concatenate(keep)

    def sharpe(x):
        sd = x.std(axis=0, ddof=1)
        return np.where(sd > 0, x.mean(axis=0) / np.where(sd > 0, sd, 1), 0.0)

    logits, degradation = [], []
    for combo in itertools.combinations(range(splits), splits // 2):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = out_of_sample(set(combo))
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
        "purge": purge,
        "embargo": embargo,
    }
