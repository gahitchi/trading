"""Stage-0 signal diagnostics (Gemini audit F-R15): what a signal predicts, before portfolio construction and
costs decide what it earns.

A full backtest mixes three things: whether the signal forecasts returns, how the portfolio turns its targets
into positions, and what trading costs. A signal with real predictive power that flips too often can lose
money net of costs; seeing only the net Sharpe, a researcher would discard the mechanism instead of slowing
the signal down. These numbers separate the three. They are informational: no gate uses them.

  information coefficient   Spearman rank correlation of the target at a close with the return over the next
                            h bars (h = 1, 5, 10, 21): across stocks each day when there are at least three,
                            otherwise over time per stock. ICIR = mean / std of the daily IC; the t-statistic
                            is Newey-West with h - 1 + bandwidth lags (overlapping horizons).
  cost drag                 active Sharpe gross of costs minus net of them (the same run, costs zeroed)
  persistence               lag-1 autocorrelation of each stock's target; holding period ~ 1 / (1 - rho)
  hit rate, payoff          share of held days whose next-day return has the position's sign; mean gain over
                            mean loss on those days

Training data only: the caller passes the training period, and every forward return ends inside it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from lab import stats

HORIZONS = (1, 5, 10, 21)


def targets_frame(signals: list) -> pd.DataFrame:
    """Dates x tickers of the targets the signal agents published (the last one per stock and day)."""
    rows = [
        (pd.Timestamp(s.ts, tz="UTC").tz_convert("America/New_York").normalize().tz_localize(None), s.ticker, s.target)
        for s in signals
    ]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "ticker", "target"])
    return df.pivot_table(index="date", columns="ticker", values="target", aggfunc="last").sort_index()


def _nw_tstat(x: np.ndarray, lags: int) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    t = len(x)
    if t < 10 or x.std() == 0:
        return float("nan")
    e = x - x.mean()
    s = e @ e / t
    for lag in range(1, min(lags, t - 1) + 1):
        s += 2 * (1 - lag / (lags + 1)) * (e[lag:] @ e[:-lag]) / t
    return float(x.mean() / math.sqrt(max(s, 1e-300) / t))


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 3:
        return float("nan")
    ra, rb = pd.Series(a[ok]).rank().to_numpy(), pd.Series(b[ok]).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def ic_profile(targets: pd.DataFrame, closes: pd.DataFrame, horizons=HORIZONS) -> dict:
    out = {}
    closes = closes.reindex(index=targets.index, columns=targets.columns)
    for h in horizons:
        fwd = closes.shift(-h) / closes - 1
        if targets.shape[1] >= 3:
            daily = np.array(
                [_spearman(targets.iloc[i].to_numpy(), fwd.iloc[i].to_numpy()) for i in range(len(targets))]
            )
            series = daily[np.isfinite(daily)]
            mean = float(series.mean()) if len(series) else float("nan")
            sd = float(series.std(ddof=1)) if len(series) > 1 else float("nan")
            out[h] = {
                "ic": mean,
                "icir": mean / sd if sd and math.isfinite(sd) and sd > 0 else float("nan"),
                "t_stat": _nw_tstat(series, h - 1 + stats.hac_lags(len(series))),
                "basis": "cross-sectional",
                "days": len(series),
            }
        else:
            per = []
            for t in targets.columns:
                a, b = targets[t].to_numpy(dtype=float), fwd[t].to_numpy(dtype=float)
                ic = _spearman(a, b)
                ok = np.isfinite(a) & np.isfinite(b)
                if math.isfinite(ic) and ok.sum() >= 10:
                    za = pd.Series(a[ok]).rank().to_numpy()
                    zb = pd.Series(b[ok]).rank().to_numpy()
                    prod = (za - za.mean()) / za.std() * (zb - zb.mean()) / zb.std()
                    per.append((ic, _nw_tstat(prod, h - 1 + stats.hac_lags(len(prod))), int(ok.sum())))
            out[h] = {
                "ic": float(np.mean([p[0] for p in per])) if per else float("nan"),
                "icir": float("nan"),
                "t_stat": float(np.mean([p[1] for p in per])) if per else float("nan"),
                "basis": "time-series",
                "days": int(sum(p[2] for p in per)),
            }
    return out


def persistence(targets: pd.DataFrame) -> dict:
    rhos = []
    for t in targets.columns:
        x = targets[t].dropna().to_numpy(dtype=float)
        if len(x) > 2 and x[1:].std() > 0 and x[:-1].std() > 0:
            rhos.append(float(np.corrcoef(x[1:], x[:-1])[0, 1]))
    rho = float(np.mean(rhos)) if rhos else float("nan")
    return {
        "autocorrelation_lag1": rho,
        "holding_period_bars": 1 / (1 - rho) if math.isfinite(rho) and rho < 1 else float("inf"),
    }


def hit_rate(targets: pd.DataFrame, closes: pd.DataFrame) -> dict:
    closes = closes.reindex(index=targets.index, columns=targets.columns)
    nxt = (closes.shift(-1) / closes - 1).to_numpy(dtype=float)
    pos = targets.to_numpy(dtype=float)
    held = np.isfinite(nxt) & np.isfinite(pos) & (pos != 0)
    pnl = (np.sign(pos) * nxt)[held]
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    return {
        "held_days": int(held.sum()),
        "hit_rate": float(len(wins) / len(pnl)) if len(pnl) else float("nan"),
        "payoff_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else float("nan"),
    }


def diagnose(cfg, spec, tickers: list[str], frames: dict, period, net_active_sharpe: float) -> dict:
    """Stage-0 diagnostics of `spec` on the (training) `period`: one run with costs zeroed records the
    targets and the gross active Sharpe."""
    from dataclasses import replace

    from lab import experiments

    experiments.guard_period(cfg, period)
    free = replace(
        cfg,
        costs=replace(cfg.costs, commission_per_share=0.0, commission_min=0.0, commission_max_pct=0.0, slippage=False),
    )
    gross = experiments.run_spec(free, spec, tickers, frames, period, keep_signals=True)
    base = experiments.run_spec(free, experiments.BASELINE, tickers, frames, period)
    active = experiments.active_returns(gross, base).to_numpy()
    gross_sharpe = stats.moments(active)[0] * math.sqrt(stats.DAYS)
    targets = targets_frame(gross.signals)
    end = pd.Timestamp(period.end) if period.end else None
    closes = pd.DataFrame({t: frames[t]["close"] for t in tickers})
    if end is not None:
        closes = closes[closes.index <= end]  # forward returns never reach past the period
    return {
        "information_coefficient": {str(h): v for h, v in ic_profile(targets, closes).items()},
        "gross_active_sharpe": gross_sharpe,
        "net_active_sharpe": net_active_sharpe,
        "cost_drag_sharpe": gross_sharpe - net_active_sharpe,
        **persistence(targets),
        **hit_rate(targets, closes),
    }


def summary(d: dict) -> dict:
    """The compact version the Researcher sees."""

    def r(v, n=3):
        return round(v, n) if isinstance(v, float) and math.isfinite(v) else None

    ic = d.get("information_coefficient", {})
    return {
        "rank_ic_by_horizon": {h: r(v.get("ic")) for h, v in ic.items()},
        "rank_ic_t_by_horizon": {h: r(v.get("t_stat"), 2) for h, v in ic.items()},
        "gross_active_sharpe": r(d.get("gross_active_sharpe")),
        "cost_drag_sharpe": r(d.get("cost_drag_sharpe")),
        "holding_period_bars": r(d.get("holding_period_bars"), 1),
        "hit_rate": r(d.get("hit_rate")),
        "payoff_ratio": r(d.get("payoff_ratio")),
    }
