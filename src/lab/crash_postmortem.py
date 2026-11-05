"""Descriptive crash-trade postmortem. No inference or strategy search.

Bar high/low excursions include the complete exit minute, so they are
bounds on *observed bars*, not realized MFE/MAE before a stop fill.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

POLICY = "crash-trade-cost-prior-trend-and-fullbar-excursion-v1"
FIELDS = ("signal", "entry", "exit", "reason", "gross", "net", "weight", "entry_reference", "fee_cost", "slippage_cost")


def _utc(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None or ts.utcoffset() != pd.Timedelta(0):
        raise ValueError("UTC-aware timestamps required")
    return ts


def attribution(trades: pd.DataFrame, bars: pd.DataFrame, side: int) -> pd.DataFrame:
    """Reconcile P&L and measure pre-signal trend and post-exit-bar extrema."""
    if type(side) is not int or side not in (-1, 1):
        raise ValueError("invalid trade direction")
    if not set(FIELDS) <= set(trades) or not {"open", "high", "low", "close"} <= set(bars):
        raise ValueError("missing input trade evidence or candle columns")
    if bars.empty or not bars.index.is_unique or not bars.index.is_monotonic_increasing or bars.index.tz is None:
        raise ValueError("minute bars must be sorted, unique and UTC aware")
    _utc(bars.index[0])
    out = []
    for trade in trades.itertuples(index=False):
        signal, entry, exit_ = [_utc(getattr(trade, k)) for k in ("signal", "entry", "exit")]
        delta = (exit_ - entry).total_seconds() / 60
        if signal > entry or delta < 1 or int(delta) != delta:
            raise ValueError("invalid or non-minute trade timeline")
        minutes = int(delta)
        window = bars.loc[(bars.index > entry) & (bars.index <= exit_), ["open", "high", "low", "close"]]
        if len(window) != minutes or not np.isfinite(window.to_numpy(dtype=float)).all():
            raise ValueError("missing or nonfinite bars during trade exposure")
        entry_ref = float(trade.entry_reference)
        high, low = float(window.high.max()), float(window.low.min())
        funding = float(getattr(trade, "funding_return", 0.0))
        funding_upper = float(getattr(trade, "funding_return_upper", funding))
        gross, net = float(trade.gross), float(trade.net)
        fees, slip = float(trade.fee_cost), float(trade.slippage_cost)
        weight = float(trade.weight)
        if not all(
            math.isfinite(v) for v in (entry_ref, high, low, funding, funding_upper, gross, net, fees, slip, weight)
        ):
            raise ValueError("nonfinite trade economics")
        if min(entry_ref, low, weight) <= 0 or funding_upper < funding:
            raise ValueError("invalid reference price, exposure or funding bounds")
        if not math.isclose(net, gross - fees - slip + funding, abs_tol=1e-9, rel_tol=1e-8):
            raise ValueError("trade P&L fails economic reconciliation")
        favorable = max(0.0, side * ((high if side > 0 else low) / entry_ref - 1))
        adverse = max(0.0, -side * ((low if side > 0 else high) / entry_ref - 1))
        prev = bars["close"].get(signal - pd.Timedelta(days=1), float("nan"))
        spot = bars["close"].get(signal, float("nan"))
        trend = float((spot / prev - 1) * 1e4) if np.isfinite(prev) and np.isfinite(spot) and prev > 0 else None
        out.append(
            {
                "signal": signal,
                "entry": entry,
                "exit": exit_,
                "reason": str(trade.reason),
                "duration_min": minutes,
                "weight": weight,
                "gross_bps": gross * 1e4,
                "net_bps": net * 1e4,
                "fee_bps": fees * 1e4,
                "slippage_bps": slip * 1e4,
                "funding_bps": funding * 1e4,
                "funding_upper_bps": funding_upper * 1e4,
                "fullbar_favorable_bps": favorable * 1e4,
                "fullbar_adverse_bps": adverse * 1e4,
                "pre_signal_24h_return_bps": trend,
                "year": signal.year,
                "hour": signal.hour,
                "ambiguous_funding_events": int(getattr(trade, "funding_ambiguous_events", 0)),
            }
        )
    return pd.DataFrame(out)


def describe(rows: pd.DataFrame) -> dict:
    """Descriptive cost decomposition. Never returns a significance claim."""
    if rows.empty:
        return {"trades": 0, "status": "no_trades"}

    def stats(part):
        return {
            "trades": len(part),
            "mean_net_bps": float(part.net_bps.mean()),
            "sum_weighted_net_bps": float((part.net_bps * part.weight).sum()),
            "mean_duration_min": float(part.duration_min.mean()),
        }

    result = {
        **stats(rows),
        "status": "descriptive_only",
        "weighted_exposure_minutes": float((rows.weight * rows.duration_min).sum()),
        "cost_decomposition_bps": {
            col: float(rows[col].sum())
            for col in ("gross_bps", "fee_bps", "slippage_bps", "funding_bps", "funding_upper_bps", "net_bps")
        },
        "ambiguous_funding_events": int(rows.ambiguous_funding_events.sum()),
        "fullbar_extrema_are_not_executable": True,
        "by_exit": {str(k): stats(part) for k, part in rows.groupby("reason")},
        "by_year": {str(k): stats(part) for k, part in rows.groupby("year")},
    }
    return result
