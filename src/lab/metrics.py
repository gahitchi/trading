"""Performance statistics from a daily equity curve."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """(deepest peak-to-trough loss as a positive fraction, longest underwater stretch in days)."""
    peak = equity.cummax()
    dd = 1 - equity / peak
    longest = cur = 0
    for under in (dd > 1e-12).to_numpy():
        cur = cur + 1 if under else 0
        longest = max(longest, cur)
    return float(dd.max()), longest


def session_closes(equity: pd.Series) -> pd.Series:
    """One mark per session: the last mark of each date (F-R05), so annualizing by 252 stays right when a
    curve carries several marks a day."""
    if not isinstance(equity.index, pd.DatetimeIndex) or not len(equity):
        return equity
    day = equity.index.normalize()
    if day.is_unique:
        return equity
    return equity.groupby(day).last()


def excess_returns(equity: pd.Series, risk_free: pd.Series | None) -> pd.Series:
    """Return per mark minus the risk-free return accrued between the two marks (calendar days, the rate known
    at the earlier mark). Without a rate series, the plain return (a 0% risk-free rate)."""
    rets = equity.pct_change().dropna()
    if risk_free is None or rets.empty:
        return rets
    rate = risk_free.reindex(equity.index, method="ffill").fillna(0.0).to_numpy()[:-1]
    days = np.diff(equity.index.to_numpy()).astype("timedelta64[D]").astype(float)
    return rets - ((1 + rate) ** (days / 365) - 1)


def summarize(
    equity: pd.Series,
    *,
    fills: list | None = None,
    gross: pd.Series | None = None,
    risk_free: pd.Series | None = None,
) -> dict:
    """equity: daily portfolio value indexed by date, first value = starting capital. Sharpe and Sortino use
    returns in excess of `risk_free` (annual rate by date, lab.data.risk_free) when given, else of 0%."""
    equity = session_closes(equity)
    values = equity.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        # Fail closed: a missing or non-finite mark means the curve is incomplete, and dropping it would
        # silently stitch over the gap (ChatGPT, G001-F2).
        bad = equity.index[~np.isfinite(values)][0]
        raise ValueError(f"equity curve has a missing or non-finite mark on {bad}; refusing to compute metrics")
    if len(equity) < 3:
        # Two marks give one return: no volatility or Sharpe can be estimated (ChatGPT, G001-F4).
        return {"days": len(equity), "insufficient": "at least 3 equity marks are needed for risk metrics"}
    if (values <= 0).any():
        return {
            "days": len(equity),
            "final_equity": float(values[-1]),
            "total_return": float(values[-1] / values[0] - 1) if values[0] > 0 else None,
            "max_drawdown": max_drawdown(equity)[0] if values[0] > 0 else None,
            "insolvent": True,
            "qualified": False,
            "insufficient": "nonpositive equity: return ratios require an explicit recapitalization model",
            **dict.fromkeys(("sharpe", "sortino", "calmar", "cagr", "volatility")),
            "fills": len(fills or []),
            "commissions": sum(f.commission for f in (fills or [])),
        }
    rets = excess_returns(equity, risk_free)
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    vol = rets.std(ddof=1) * math.sqrt(TRADING_DAYS)
    # Sortino downside deviation: RMS of min(r, 0) over ALL observations (target 0), not the std of the
    # negative subset. Fix contributed by ChatGPT (hardening branch, cab8cef).
    downside = math.sqrt(float(np.mean(np.minimum(rets.to_numpy(), 0.0) ** 2))) * math.sqrt(TRADING_DAYS)
    mean = rets.mean() * TRADING_DAYS
    mdd, mdd_days = max_drawdown(equity)
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    out = {
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "days": len(equity),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return": float(total),
        "cagr": float(cagr),
        "volatility": float(vol),
        "sharpe": float(mean / vol) if vol > 0 else 0.0,
        "sortino": float(mean / downside) if downside and downside > 0 else 0.0,
        "max_drawdown": mdd,
        "max_drawdown_days": mdd_days,
        "calmar": float(cagr / mdd) if mdd > 0 else 0.0,
        "risk_free": "series" if risk_free is not None else "zero",
    }
    if gross is not None and len(gross):
        out["avg_exposure"] = float(gross.mean())
    if fills is not None:
        traded = sum(f.qty * f.price for f in fills)
        out["fills"] = len(fills)
        out["commissions"] = round(sum(f.commission for f in fills), 2)
        out["turnover_per_year"] = float(traded / equity.mean() / years) if years > 0 else 0.0
    return out


def fmt(m: dict) -> dict:
    """Human-readable version for tables."""
    pct = ("total_return", "cagr", "volatility", "max_drawdown", "avg_exposure")
    return {
        k: (f"{v:+.1%}" if k in ("total_return", "cagr") else f"{v:.1%}")
        if k in pct and isinstance(v, float)
        else (f"{v:.2f}" if isinstance(v, float) else v)
        for k, v in m.items()
    }
