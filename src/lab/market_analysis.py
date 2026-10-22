"""Deterministic, point-in-time *descriptive* analysis of a daily equity panel.

No predictive claims, order signals, optimizer, broker access, imputation or
automatic stock selection. A valid report is EXPLORATORY_ONLY until independent
data licensing, corporate-action and point-in-time-universe checks are proven.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

TRADING_DAYS = 252
REQUIRED = ("open", "high", "low", "close", "volume")


class MarketAnalysisError(ValueError):
    """Incomplete, contradictory or unsupported inputs cannot be analyzed."""


@dataclass(frozen=True, slots=True)
class AnalysisSpec:
    lookback: int = 252
    risk_window: int = 63
    momentum_days: int = 252
    skip_recent: int = 21
    liquidity_window: int = 20
    covariance_shrinkage: float = 0.15
    tail_level: float = 0.95

    def __post_init__(self):
        for key in ("lookback", "risk_window", "momentum_days", "skip_recent", "liquidity_window"):
            value = getattr(self, key)
            if type(value) is not int or value < (0 if key == "skip_recent" else 1):
                raise MarketAnalysisError(f"{key} must be a valid nonnegative integer")
        if self.lookback < max(self.risk_window, self.momentum_days, self.liquidity_window, 63):
            raise MarketAnalysisError("lookback must cover the longest requested feature and at least 63 sessions")
        if self.risk_window < 3 or self.momentum_days <= self.skip_recent:
            raise MarketAnalysisError("risk_window must be >= 3 and momentum_days > skip_recent")
        if not 0 <= self.covariance_shrinkage <= 1 or not math.isfinite(self.covariance_shrinkage):
            raise MarketAnalysisError("covariance_shrinkage must be finite in [0, 1]")
        if not 0.5 < self.tail_level < 1 or not math.isfinite(self.tail_level):
            raise MarketAnalysisError("tail_level must be finite in (0.5, 1)")


def _panel(frames: dict[str, pd.DataFrame], as_of: date, spec: AnalysisSpec) -> dict[str, pd.DataFrame]:
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise MarketAnalysisError("as_of must be an explicit exchange-session date")
    if not frames:
        raise MarketAnalysisError("analyze requires at least one instrument")
    result = {}
    for ticker, frame in sorted(frames.items()):
        if not isinstance(ticker, str) or not ticker.strip() or not isinstance(frame, pd.DataFrame):
            raise MarketAnalysisError("instruments must have identifiers and DataFrame OHLCV histories")
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is not None:
            raise MarketAnalysisError(f"{ticker}: index must contain timezone-naive daily sessions")
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise MarketAnalysisError(f"{ticker}: duplicate or unsorted session dates")
        if not frame.index.equals(frame.index.normalize()):
            raise MarketAnalysisError(f"{ticker}: index must be normalized exchange-session dates")
        if any(col not in frame.columns for col in REQUIRED):
            raise MarketAnalysisError(f"{ticker}: missing OHLCV columns")
        # Filter FIRST. Never inspect or validate rows after the historical cutoff.
        recent = frame.loc[frame.index <= pd.Timestamp(as_of), list(REQUIRED)].tail(spec.lookback + 1).copy()
        if len(recent) < spec.lookback + 1:
            raise MarketAnalysisError(f"{ticker}: requires {spec.lookback + 1} complete observations by {as_of}")
        if recent.index[-1].date() != as_of:
            raise MarketAnalysisError(f"{ticker}: no observation for requested session {as_of}")
        try:
            matrix = recent.to_numpy(dtype=float)
        except (TypeError, ValueError) as exc:
            raise MarketAnalysisError(f"{ticker}: nonnumeric OHLCV observations") from exc
        if not np.isfinite(matrix).all():
            raise MarketAnalysisError(f"{ticker}: missing/nonfinite OHLCV observations; no silent imputation")
        opn, high, low, close, volume = matrix.T
        if (
            (opn <= 0).any()
            or (close <= 0).any()
            or (low <= 0).any()
            or (volume < 0).any()
            or (high < np.maximum(opn, close)).any()
            or (low > np.minimum(opn, close)).any()
            or (high < low).any()
        ):
            raise MarketAnalysisError(f"{ticker}: invalid prices, volume or OHLC extremes")
        result[ticker] = recent.astype(float)
    baseline = next(iter(result.values())).index
    if any(not df.index.equals(baseline) for df in result.values()):
        raise MarketAnalysisError("unaligned exchange sessions; refusing fill-forward or row dropping")
    return result


def _finite(value: float, label: str) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise MarketAnalysisError(f"nonfinite computed {label}")
    return v


def _asset_stats(frame: pd.DataFrame, spec: AnalysisSpec) -> dict:
    close = frame["close"].to_numpy(dtype=float)
    volume = frame["volume"].to_numpy(dtype=float)
    rets = close[1:] / close[:-1] - 1
    risk = rets[-spec.risk_window :]
    vol = _finite(risk.std(ddof=1) * math.sqrt(TRADING_DAYS), "volatility")
    down = _finite(np.sqrt(np.mean(np.minimum(risk, 0.0) ** 2)) * math.sqrt(TRADING_DAYS), "downside")
    full = rets
    losses = -full
    tail_threshold = _finite(np.quantile(losses, spec.tail_level), "value at risk")
    var = max(0.0, tail_threshold)
    es = max(0.0, _finite(losses[losses >= tail_threshold].mean(), "expected shortfall"))
    peak = np.maximum.accumulate(close)
    mdd = _finite(np.max(1 - close / peak), "drawdown")
    latest_dd = _finite(1 - close[-1] / peak[-1], "drawdown")
    short = _finite(close[-1] / close[-22] - 1, "21-day momentum")
    momentum = _finite(
        close[-1 - spec.skip_recent] / close[-1 - spec.momentum_days] - 1, "skip-month momentum"
    )
    dollar_volume = close[-spec.liquidity_window :] * volume[-spec.liquidity_window :]
    return {
        "close": _finite(close[-1], "close"),
        "momentum_21d": short,
        "momentum_skip_recent": momentum,
        "realized_vol_annual": vol,
        "downside_vol_annual": down,
        "historical_var_loss": var,
        "historical_expected_shortfall_loss": es,
        "max_drawdown_in_window": mdd,
        "current_drawdown": latest_dd,
        "median_dollar_volume": _finite(np.median(dollar_volume), "dollar volume"),
        "last_volume": _finite(volume[-1], "volume"),
    }


def analyze_market(
    frames: dict[str, pd.DataFrame],
    *,
    as_of: date,
    spec: AnalysisSpec | None = None,
    provenance: dict | None = None,
) -> dict:
    """Build reproducible *as-of-close* market diagnostics; never a trade permit.

    We never auto-align missing sessions, extrapolate prices, guess membership,
    use post-cutoff rows or silently use NaN as a valid research result.
    """
    spec = spec or AnalysisSpec()
    selected = _panel(frames, as_of, spec)
    tickers = list(selected)
    returns = np.column_stack(
        [
            f["close"].to_numpy(dtype=float)[1:] / f["close"].to_numpy(dtype=float)[:-1] - 1
            for f in selected.values()
        ]
    )
    cov_raw = np.atleast_2d(np.cov(returns[-spec.risk_window :], rowvar=False, ddof=1))
    diagonal = np.diag(np.diag(cov_raw))
    cov = ((1 - spec.covariance_shrinkage) * cov_raw + spec.covariance_shrinkage * diagonal) * TRADING_DAYS
    cov = (cov + cov.T) / 2
    eig = np.linalg.eigvalsh(cov)
    if eig.min() < -1e-10:
        raise MarketAnalysisError("invalid non-PSD covariance estimate")
    std = np.sqrt(np.maximum(np.diag(cov), 0))
    corr = []
    for i in range(len(tickers)):
        row = []
        for j in range(len(tickers)):
            if std[i] == 0 or std[j] == 0:
                row.append(None)
            else:
                row.append(_finite(cov[i, j] / (std[i] * std[j]), "correlation"))
        corr.append(row)
    stats = {t: _asset_stats(f, spec) for t, f in selected.items()}
    active = sum(v["momentum_21d"] > 0 for v in stats.values())
    long_term = sum(v["momentum_skip_recent"] > 0 for v in stats.values())
    breadth = active / len(tickers)
    # Deliberately descriptive; not trained probabilities or an investment signal.
    if breadth >= 0.7:
        descriptive_regime = "BROAD_UPTREND"
    elif breadth <= 0.3:
        descriptive_regime = "BROAD_DOWNTREND"
    else:
        descriptive_regime = "MIXED"
    warnings = [
        "Exploratory-only: OHLCV data cannot establish point-in-time membership, corporate actions, finality or fillability",
        "Historical VaR/ES and shrunk covariance describe this sample; they do not predict future tail events",
    ]
    if any(v["median_dollar_volume"] <= 0 for v in stats.values()):
        warnings.append("Zero-dollar-volume instrument: not evidence of executable liquidity")
    if any(v["realized_vol_annual"] == 0 for v in stats.values()):
        warnings.append("Zero-variance instrument: correlation is undefined (null)")
    source = provenance or {}
    raw_input = hashlib.sha256()
    for ticker, df in selected.items():
        raw_input.update(ticker.encode())
        raw_input.update(df.to_csv(index=True, float_format="%.17g", date_format="%Y-%m-%d").encode())
    payload = {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "tickers": tickers,
        "spec": asdict(spec),
        "data_sha256": raw_input.hexdigest(),
        "source_manifest": {t: source.get(t) for t in tickers},
    }
    analysis_id = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return {
        **payload,
        "analysis_id": analysis_id,
        "qualification": "EXPLORATORY_ONLY",
        "observations_per_instrument": spec.lookback + 1,
        "features": stats,
        "market": {
            "breadth_positive_21d": breadth,
            "breadth_positive_skip_recent": long_term / len(tickers),
            "descriptive_regime": descriptive_regime,
            "median_asset_vol_annual": _finite(
                np.median([v["realized_vol_annual"] for v in stats.values()]), "median volatility"
            ),
        },
        "risk": {
            "tickers": tickers,
            "annualized_covariance_shrunk_to_diagonal": cov.tolist(),
            "correlation": corr,
            "shrinkage": spec.covariance_shrinkage,
            "min_covariance_eigenvalue": _finite(eig.min(), "covariance eigenvalue"),
        },
        "warnings": warnings,
    }
