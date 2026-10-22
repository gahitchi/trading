"""Causal research forecasts, calibrated uncertainty and filtered volatility regimes.

Read-only exploratory analytics; no strategies, account access or order permits.
Training, calibration and test blocks are chronological and labels are purged
across boundaries by the full forecast horizon.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

from lab.market_analysis import AnalysisSpec, MarketAnalysisError, _panel

FEATURE_NAMES = (
    "log_return_1d", "log_momentum_5d", "log_momentum_21d",
    "log_momentum_63d", "realized_vol_20d", "downside_rms_20d",
    "drawdown_63d", "volume_log_z_20d", "overnight_log_gap", "intraday_log_range",
)
WARMUP = 63


@dataclass(frozen=True, slots=True)
class ForecastSpec:
    horizon: int = 1
    min_train: int = 252
    calibration: int = 60
    test: int = 60
    ridge_alpha: float = 10.0
    coverage: float = 0.90
    drift_z_limit: float = 5.0

    def __post_init__(self):
        for field in ("horizon", "min_train", "calibration", "test"):
            v = getattr(self, field)
            if type(v) is not int or v < 1:
                raise MarketAnalysisError(f"{field} must be a positive integer")
        if self.min_train < 100 or self.calibration < 20 or self.test < 20:
            raise MarketAnalysisError("at least 100 training, 20 calibration and 20 test labels required")
        if self.horizon > 20:
            raise MarketAnalysisError("forecast horizon must be <=20 exchange sessions")
        if not math.isfinite(self.ridge_alpha) or self.ridge_alpha <= 0:
            raise MarketAnalysisError("ridge_alpha must be finite and strictly positive")
        if not math.isfinite(self.coverage) or not 0.5 < self.coverage < 1:
            raise MarketAnalysisError("coverage must lie strictly between 0.5 and 1")
        if math.ceil((self.calibration + 1) * self.coverage) > self.calibration:
            raise MarketAnalysisError("calibration size is too small for requested conformal coverage")
        if not math.isfinite(self.drift_z_limit) or self.drift_z_limit <= 0:
            raise MarketAnalysisError("drift_z_limit must be finite and positive")


def _features(frame: pd.DataFrame) -> np.ndarray:
    """Feature row i contains only OHLCV from sessions <=i."""
    close = frame["close"].to_numpy(dtype=float)
    opn = frame["open"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    volume = np.log1p(frame["volume"].to_numpy(dtype=float))
    returns = np.diff(np.log(close))
    out = []
    for i in range(WARMUP, len(frame)):
        recent = returns[i - 20 : i]
        vwindow = volume[i - 20 : i + 1]
        vstd = float(vwindow.std(ddof=1))
        out.append(
            (
                returns[i - 1],
                math.log(close[i] / close[i - 5]),
                math.log(close[i] / close[i - 21]),
                math.log(close[i] / close[i - 63]),
                float(np.sqrt(np.mean(recent**2))),
                float(np.sqrt(np.mean(np.minimum(recent, 0) ** 2))),
                close[i] / float(np.max(close[i - 63 : i + 1])) - 1,
                float((volume[i] - vwindow.mean()) / vstd) if vstd > 1e-12 else 0.0,
                math.log(opn[i] / close[i - 1]),
                math.log(high[i] / low[i]),
            )
        )
    matrix = np.asarray(out, dtype=float)
    if not np.isfinite(matrix).all():
        raise MarketAnalysisError("nonfinite causal feature; abort forecast")
    return matrix


def _fit_ridge(x: np.ndarray, y: np.ndarray, penalty: float) -> dict:
    mu = x.mean(axis=0)
    sigma = x.std(axis=0)
    sigma = np.where(sigma > 1e-12, sigma, 1.0)
    z = (x - mu) / sigma
    ymean = float(y.mean())
    beta = np.linalg.solve(z.T @ z + np.eye(z.shape[1]) * penalty, z.T @ (y - ymean))
    if not np.isfinite(beta).all():
        raise MarketAnalysisError("ridge model numerical instability")
    return {"mean": mu, "scale": sigma, "coef": beta, "intercept": ymean}


def _predict(model: dict, x: np.ndarray) -> np.ndarray:
    return model["intercept"] + ((x - model["mean"]) / model["scale"]) @ model["coef"]


def _calibration(model: dict, x: np.ndarray, y: np.ndarray, coverage: float) -> tuple[float, np.ndarray]:
    residuals = y - _predict(model, x)
    # Split conformal uses an exchangeability assumption; dependent market
    # observations do not automatically satisfy nominal marginal coverage.
    rank = math.ceil((len(residuals) + 1) * coverage)
    if rank > len(residuals):
        raise MarketAnalysisError("calibration cannot support the selected nominal coverage")
    q = float(np.partition(np.abs(residuals), rank - 1)[rank - 1])
    if not np.isfinite(q):
        raise MarketAnalysisError("nonfinite calibration score")
    return q, residuals


def _prob_up(pred: np.ndarray, residuals: np.ndarray) -> np.ndarray:
    return np.mean(pred[:, None] + residuals[None, :] > 0, axis=1)


def _markov_volatility_filter(training_returns: np.ndarray, observations: np.ndarray) -> dict:
    """Training-estimated two-state Markov volatility filter, not a price oracle."""
    width = 20
    if len(training_returns) < width + 50:
        raise MarketAnalysisError("insufficient training history for volatility regimes")
    rolling = np.asarray(
        [
            np.sqrt(np.mean(training_returns[i - width + 1 : i + 1] ** 2))
            for i in range(width - 1, len(training_returns))
        ]
    )
    threshold = float(np.median(rolling))
    states = (rolling > threshold).astype(int)
    transitions = np.ones((2, 2), dtype=float)
    for a, b in itertools.pairwise(states):
        transitions[a, b] += 1
    transitions /= transitions.sum(axis=1, keepdims=True)
    paired = training_returns[width - 1 :]
    sigmas = np.asarray(
        [
            max(float(np.sqrt(np.mean(paired[states == k] ** 2))), 1e-6)
            if np.any(states == k) else 1e-6
            for k in range(2)
        ]
    )
    posterior = np.asarray([0.5, 0.5])
    for r in observations:
        prior = posterior @ transitions
        log_em = -np.log(sigmas) - 0.5 * (float(r) / sigmas) ** 2
        weighted = prior * np.exp(log_em - np.max(log_em))
        posterior = weighted / weighted.sum()
    next_prob = posterior @ transitions
    return {
        "high_volatility_probability_filtered": float(posterior[1]),
        "next_session_high_volatility_probability_estimate": float(next_prob[1]),
        "estimated_sigma_low_daily": float(sigmas[0]),
        "estimated_sigma_high_daily": float(sigmas[1]),
        "training_volatility_median": threshold,
        "transition_probabilities": transitions.tolist(),
        "interpretation": "empirical two-state filtered volatility estimate; not validated as a price forecast",
    }


def forecast_research(
    ticker: str, frame: pd.DataFrame, *, as_of: date, spec: ForecastSpec | None = None,
    provenance: dict | None = None,
) -> dict:
    """One purged chronological OOS test and an independently re-fitted as-of forecast."""
    spec = spec or ForecastSpec()
    if not isinstance(ticker, str) or not ticker.strip():
        raise MarketAnalysisError("nonempty instrument required")
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise MarketAnalysisError("as_of must be an explicit exchange-session date")
    if not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex):
        raise MarketAnalysisError("forecast needs an indexed daily OHLCV frame")
    count = int((frame.index <= pd.Timestamp(as_of)).sum())
    if count < 64:
        raise MarketAnalysisError("insufficient historical OHLCV for causal features")
    clean = _panel({ticker: frame}, as_of, AnalysisSpec(lookback=count - 1, momentum_days=63))[ticker]
    x_all = _features(clean)
    close = clean["close"].to_numpy(dtype=float)
    n = len(close)
    # Label at feature close[t] is known only after close[t+h].
    labels = np.log(close[WARMUP + spec.horizon :] / close[WARMUP : n - spec.horizon])
    x_labeled = x_all[: len(labels)]
    nlab = len(labels)
    test_start = nlab - spec.test
    calibration_end = test_start - spec.horizon
    calibration_start = calibration_end - spec.calibration
    train_end = calibration_start - spec.horizon
    if train_end < spec.min_train:
        minimum = WARMUP + spec.horizon * 3 + spec.min_train + spec.calibration + spec.test
        raise MarketAnalysisError(f"insufficient leakage-purged history: need at least {minimum} sessions")
    model = _fit_ridge(x_labeled[:train_end], labels[:train_end], spec.ridge_alpha)
    q, calres = _calibration(
        model, x_labeled[calibration_start:calibration_end],
        labels[calibration_start:calibration_end], spec.coverage,
    )
    testpred = _predict(model, x_labeled[test_start:])
    testtrue = labels[test_start:]
    p_up = _prob_up(testpred, calres)
    baseline_mean = float(labels[:train_end].mean())
    base_prob_up = float(np.mean(labels[:train_end] > 0))
    mae = float(np.mean(np.abs(testtrue - testpred)))
    baseline_mae = float(np.mean(np.abs(testtrue - baseline_mean)))
    coverage_empirical = float(np.mean(np.abs(testtrue - testpred) <= q))
    brier = float(np.mean((p_up - (testtrue > 0)) ** 2))
    baseline_brier = float(np.mean((base_prob_up - (testtrue > 0)) ** 2))
    # Refit only using known labels; reserve a fresh calibration block.
    recent_cal_end = nlab
    recent_cal_start = recent_cal_end - spec.calibration
    recent_train_end = recent_cal_start - spec.horizon
    if recent_train_end < spec.min_train:
        raise MarketAnalysisError("insufficient refit history")
    current_model = _fit_ridge(x_labeled[:recent_train_end], labels[:recent_train_end], spec.ridge_alpha)
    current_q, current_res = _calibration(
        current_model, x_labeled[recent_cal_start:recent_cal_end],
        labels[recent_cal_start:recent_cal_end], spec.coverage,
    )
    point = float(_predict(current_model, x_all[-1:])[0])
    prob = float(_prob_up(np.asarray([point]), current_res)[0])
    z = np.abs((x_all[-1] - current_model["mean"]) / current_model["scale"])
    max_z = float(np.max(z))
    status = "ABSTAIN_FEATURE_DRIFT" if max_z > spec.drift_z_limit else "EXPLORATORY_ONLY"
    regime_train_returns = np.diff(np.log(close[: WARMUP + train_end + 1]))
    regime_obs = np.diff(np.log(close))
    regime = _markov_volatility_filter(regime_train_returns, regime_obs)
    data_hash = hashlib.sha256(
        clean.to_csv(index=True, date_format="%Y-%m-%d", float_format="%.17g").encode()
    ).hexdigest()
    config = {
        "version": 1, "ticker": ticker, "as_of": as_of.isoformat(),
        "data_sha256": data_hash, "spec": asdict(spec),
        "source": provenance or {},
    }
    model_hash = hashlib.sha256(
        json.dumps(
            {
                "features": FEATURE_NAMES, "coef": current_model["coef"].tolist(),
                "center": current_model["mean"].tolist(), "scale": current_model["scale"].tolist(),
                "intercept": current_model["intercept"], "train_end": recent_train_end,
            }, sort_keys=True, allow_nan=False,
        ).encode()
    ).hexdigest()
    config["model_sha256"] = model_hash
    result_hash = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()
    dates = clean.index
    return {
        **config,
        "analysis_id": result_hash,
        "qualification": "RESEARCH_ONLY_NOT_FOR_TRADING",
        "forecast_status": status,
        "horizon_sessions": spec.horizon,
        "target_definition": "future close-to-close log return; forecast issued after current bar close",
        "frozen_oos_test": {
            "first_feature_session": str(dates[WARMUP + test_start].date()),
            "last_feature_session": str(dates[WARMUP + nlab - 1].date()),
            "train_count": train_end, "calibration_count": spec.calibration,
            "test_count": spec.test, "purge_sessions": spec.horizon,
            "model_mae_log_return": mae,
            "train_mean_baseline_mae_log_return": baseline_mae,
            "model_improves_baseline_mae": bool(mae < baseline_mae),
            "nominal_interval_coverage": spec.coverage,
            "observed_interval_coverage": coverage_empirical,
            "empirical_up_probability_brier": brier,
            "train_frequency_baseline_brier": baseline_brier,
        },
        "as_of_forecast": {
            "expected_return_fraction": math.expm1(point),
            "lower_return_fraction": math.expm1(point - current_q),
            "upper_return_fraction": math.expm1(point + current_q),
            "empirical_probability_positive": prob,
            "probability_calibration_note": "empirical residual estimate; temporal dependence may invalidate calibration",
            "conformal_quantile_log_return": current_q,
            "abstain_on_feature_drift": bool(max_z > spec.drift_z_limit),
            "max_training_standardized_feature_distance": max_z,
            "most_shifted_feature": FEATURE_NAMES[int(np.argmax(z))],
        },
        "volatility_regime": regime,
        "warnings": [
            "Exploratory only: vendor revision times, corporate actions and delisted universe are not verified",
            "One chronological OOS split is not proof of robust predictability or deployable edge",
            "Split conformal intervals lack finite-sample coverage guarantee under serial dependence",
            "Estimated regime probabilities assume stationary transition and Gaussian emission approximations",
            "A forecast is neither a portfolio target nor an execution authorization",
        ],
    }
