"""
Scientific procedures built on the backtester:

- compare():    each signal as an equal-weight portfolio vs the buy-and-hold control
- null_test():  the noise floor. Repeats the comparison on block-bootstrapped copies
                of the real data (real return distribution, no long-range pattern)
                and reports how often pure noise produced an edge at least as large
                as the real one (a p-value)
- holdout ledger: the holdout period may be used once per hypothesis; every use is
                recorded, and repeats need an explicit, logged reason
- trial registry: every configuration evaluated on a dataset, so the Deflated Sharpe
                Ratio can charge for all of them (results/trials.jsonl)
- pbo_study(), walk_forward(): parameter studies (overfitting probability, rolling
                out-of-sample optimization)
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from lab import backtest, data, stats
from lab.config import LabConfig, Period, RiskSettings
from lab.signals.registry import SignalSpec

BASELINE = SignalSpec("buy_and_hold")


def effective_risk(cfg: LabConfig, n_tickers: int) -> RiskSettings:
    """Risk settings for research runs, which measure the STRATEGY, not the risk policy:

    - No per-stock cap: a cap would trim the strategy's own weights (an equal-weight
      control, or a top-3 ranking at 33% each) and the comparison would measure the cap.
      Gross exposure stays capped at max_gross (no leverage).
    - The drawdown circuit breaker is off: once tripped it would turn the buy-and-hold
      control into "buy-and-hold until the first crash, then cash".
    Both are always enforced in paper trading.
    """
    del n_tickers  # kept for API stability
    return RiskSettings(1.0, cfg.risk.max_gross, 0.9999)


def run_spec(
    cfg: LabConfig, spec: SignalSpec, tickers: list[str], frames: dict, period: Period, label: str | None = None
) -> backtest.RunResult:
    """Equal-weight portfolio of `tickers`. With a point-in-time membership file, each day's
    capital is shared among the stocks that were in the universe that day."""
    first = spec.make()
    if getattr(first, "cross_sectional", False):
        sleeves, xs = [], [(first, 1.0, list(tickers))]
    else:
        w = 1.0 / len(tickers)
        sleeves, xs = (
            [backtest.Sleeve(first, tickers[0], w)] + [backtest.Sleeve(spec.make(), t, w) for t in tickers[1:]],
            [],
        )
    return backtest.run(
        cfg,
        sleeves,
        frames,
        period.start,
        period.end,
        label=label or first.label(),
        risk=effective_risk(cfg, len(tickers)),
        membership=data.membership(cfg),
        normalize_active=True,
        cross_sectional=xs,
        target_vol=cfg.portfolio.target_vol,
    )


@dataclass
class Comparison:
    period: str
    start: str
    end: str
    tickers: list
    rows: list  # dicts: label, metrics..., sharpe_edge, return_edge
    data_quality: dict | None = None

    def table(self) -> pd.DataFrame:
        cols = [
            "label",
            "total_return",
            "cagr",
            "sharpe",
            "max_drawdown",
            "avg_exposure",
            "fills",
            "commissions",
            "sharpe_edge",
            "dsr",
        ]
        df = pd.DataFrame(self.rows)
        return df[[c for c in cols if c in df.columns]]


def research_tickers(cfg: LabConfig) -> tuple[list[str], list[str]]:
    """(tickers with data, tickers listed but missing data). The universe is the
    point-in-time membership file when present, else [universe] in lab.toml."""
    have = data.manifest(cfg)
    m = data.membership(cfg)
    listed = sorted(m) if m is not None else list(cfg.universe)
    return [t for t in listed if t in have], [t for t in listed if t not in have]


# ------------------------------------------------------------------------- trials


def active_returns(result: backtest.RunResult, baseline: backtest.RunResult) -> pd.Series:
    """Daily return of the strategy minus that of buy-and-hold: the edge itself."""
    a = result.equity.pct_change()
    b = baseline.equity.pct_change()
    both = pd.concat([a, b], axis=1, join="inner").dropna()
    return both.iloc[:, 0] - both.iloc[:, 1]


def dataset_key(period: Period, start: str, end: str, tickers: list[str]) -> str:
    return f"{period.name}|{start}|{end}|{','.join(sorted(tickers))}"


def _trials_path(cfg: LabConfig) -> Path:
    return cfg.results_dir / "trials.jsonl"


def register_trial(cfg: LabConfig, key: str, spec: SignalSpec, label: str, active: pd.Series) -> None:
    """Every configuration ever evaluated on a dataset is a trial; the Deflated Sharpe
    Ratio charges for all of them. Re-running an identical configuration is not a new trial."""
    sr, *_ = stats.moments(active.to_numpy())
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    with _trials_path(cfg).open("a") as f:
        f.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "dataset": key,
                    "fingerprint": spec_fingerprint(spec),
                    "label": label,
                    "sharpe_daily": sr,
                }
            )
            + "\n"
        )


def trial_sharpes(cfg: LabConfig, key: str) -> list[float]:
    p = _trials_path(cfg)
    if not p.exists():
        return []
    latest = {}
    for line in p.read_text().splitlines():
        t = json.loads(line)
        if t["dataset"] == key:
            latest[t["fingerprint"]] = t["sharpe_daily"]
    return list(latest.values())


def compare(
    cfg: LabConfig,
    specs: list[SignalSpec],
    tickers: list[str],
    period: Period,
    frames: dict | None = None,
    *,
    register: bool = True,
) -> tuple[Comparison, list[backtest.RunResult]]:
    frames = frames or data.load_many(cfg, tickers)
    results = [run_spec(cfg, BASELINE, tickers, frames, period, "buy_and_hold (control)")]
    cand_specs = [s for s in specs if s != BASELINE]
    results += [run_spec(cfg, s, tickers, frames, period) for s in cand_specs]
    end = period.end or max(f.index[-1] for f in frames.values()).date()
    key = dataset_key(period, str(period.start), str(end), tickers)

    actives = [active_returns(r, results[0]) for r in results[1:]]
    if register:
        for s, r, act in zip(cand_specs, results[1:], actives, strict=True):
            register_trial(cfg, key, s, r.label, act)
    trials = trial_sharpes(cfg, key) or [stats.moments(a.to_numpy())[0] for a in actives]

    base = results[0].metrics
    rows = []
    for i, r in enumerate(results):
        m = dict(r.metrics)
        m["label"] = r.label
        m["sharpe_edge"] = m.get("sharpe", 0) - base.get("sharpe", 0)
        m["return_edge"] = m.get("total_return", 0) - base.get("total_return", 0)
        m["signal_errors"] = r.signal_errors
        m["rejections"] = r.rejections
        m["risk_events"] = [e.kind for e in r.risk_events]
        if i > 0:
            act = actives[i - 1].to_numpy()
            d = stats.dsr(act, trials)
            lo, hi = stats.sharpe_ci(act)
            m.update(
                active_sharpe=d["sharpe_annual"],
                psr=d["psr"],
                dsr=d["dsr"],
                trials=d["trials"],
                active_sharpe_ci=[lo, hi],
                expected_max_sharpe=d["expected_max_sharpe_annual"],
            )
        rows.append(m)
    return Comparison(period.name, str(period.start), str(end), tickers, rows, data.quality(cfg, tickers)), results


# ------------------------------------------------------------------------- parameter studies


def grid_specs(name: str, grid: dict[str, list], research_dir: Path) -> list[SignalSpec]:
    """Every valid combination of parameter values (invalid combinations are skipped)."""
    from lab.signals.registry import resolve

    keys = sorted(grid)
    out = []
    for values in itertools.product(*(grid[k] for k in keys)):
        try:
            out.append(resolve(name, research_dir, dict(zip(keys, values, strict=True))))
        except ValueError:
            continue
    if not out:
        raise ValueError(f"no valid parameter combination in {grid}")
    return out


def active_matrix(
    cfg: LabConfig, specs: list[SignalSpec], tickers: list[str], period: Period, frames: dict
) -> pd.DataFrame:
    """Daily active returns (vs buy-and-hold) of each configuration, one column each."""
    _, results = compare(cfg, specs, tickers, period, frames)
    cols = {r.label: active_returns(r, results[0]) for r in results[1:]}
    return pd.DataFrame(cols).dropna()


def pbo_study(
    cfg: LabConfig, specs: list[SignalSpec], tickers: list[str], period: Period, frames: dict, splits: int = 10
) -> dict:
    m = active_matrix(cfg, specs, tickers, period, frames)
    out = stats.pbo(m.to_numpy(), splits=splits)
    sr = {c: stats.moments(m[c].to_numpy())[0] * stats.math.sqrt(stats.DAYS) for c in m.columns}
    out["active_sharpe_by_config"] = dict(sorted(sr.items(), key=lambda kv: -kv[1]))
    return out


def walk_forward(
    cfg: LabConfig,
    specs: list[SignalSpec],
    tickers: list[str],
    start: date,
    end: date,
    frames: dict,
    is_years: int = 4,
    oos_years: int = 1,
) -> dict:
    """Rolling optimization: pick the best configuration (active Sharpe) on each
    in-sample window, then trade it, unchanged, on the following out-of-sample window.
    The stitched out-of-sample record is what the optimization procedure really delivers."""
    windows = []
    ws = pd.Timestamp(start)
    while True:
        is_end = ws + pd.DateOffset(years=is_years) - pd.Timedelta(days=1)
        oos_end = is_end + pd.DateOffset(years=oos_years)
        if oos_end > pd.Timestamp(end) + pd.Timedelta(days=1):
            break
        windows.append((ws.date(), is_end.date(), (is_end + pd.Timedelta(days=1)).date(), oos_end.date()))
        ws = ws + pd.DateOffset(years=oos_years)
    if not windows:
        raise ValueError("period too short for one in-sample + out-of-sample window")

    steps, oos_active, oos_strategy = [], [], []
    for i, (a, b, c, d) in enumerate(windows):
        ins = Period(f"wfa-is-{i}", a, b)
        m = active_matrix(cfg, specs, tickers, ins, frames)
        is_sr = {col: stats.moments(m[col].to_numpy())[0] * stats.math.sqrt(stats.DAYS) for col in m.columns}
        best_label = max(is_sr, key=is_sr.get)
        best = specs[list(m.columns).index(best_label)]
        oos = Period(f"wfa-oos-{i}", c, d)
        comp, results = compare(cfg, [best], tickers, oos, frames, register=False)
        act = active_returns(results[1], results[0])
        oos_active.append(act)
        oos_strategy.append(results[1].equity.pct_change().dropna())
        steps.append(
            {
                "in_sample": f"{a}..{b}",
                "out_of_sample": f"{c}..{d}",
                "chosen": best_label,
                "is_active_sharpe": is_sr[best_label],
                "oos_active_sharpe": stats.moments(act.to_numpy())[0] * stats.math.sqrt(stats.DAYS),
                "oos_return": comp.rows[1].get("total_return"),
                "oos_buy_and_hold": comp.rows[0].get("total_return"),
            }
        )
    act_all = pd.concat(oos_active)
    d = stats.dsr(act_all.to_numpy(), [stats.moments(act_all.to_numpy())[0]])
    lo, hi = stats.sharpe_ci(act_all.to_numpy())
    is_mean = float(np.mean([s["is_active_sharpe"] for s in steps]))
    return {
        "windows": steps,
        "oos_active_sharpe": d["sharpe_annual"],
        "oos_active_sharpe_ci": [lo, hi],
        "oos_psr": d["psr"],
        "efficiency": d["sharpe_annual"] / is_mean if is_mean > 0 else float("nan"),
        "distinct_choices": len({s["chosen"] for s in steps}),
    }


# ------------------------------------------------------------------------- noise floor


def _null_worker(args):
    cfg_path, spec, tickers, frames, period, seed = args
    from lab.config import load_config

    cfg = load_config(cfg_path)
    boot = data.block_bootstrap_panel({t: frames[t] for t in tickers}, seed)
    cand = run_spec(cfg, spec, tickers, boot, period).metrics
    base = run_spec(cfg, BASELINE, tickers, boot, period).metrics
    return cand.get("sharpe", 0) - base.get("sharpe", 0)


def null_test(
    cfg: LabConfig,
    spec: SignalSpec,
    tickers: list[str],
    period: Period,
    samples: int,
    frames: dict | None = None,
    workers: int | None = None,
    observed_edge: float | None = None,
) -> dict:
    frames = frames or data.load_many(cfg, tickers)
    if not frames:
        raise data.DataError("null test requires price data")
    reference = frames[tickers[0]].index
    if any(not frames[t].index.equals(reference) for t in tickers):
        raise data.DataError(
            "null test requires a date-aligned panel: asynchronous bootstrap destroys cross-asset correlations"
        )
    if observed_edge is None:
        comp, _ = compare(cfg, [spec], tickers, period, frames)
        observed_edge = comp.rows[1]["sharpe_edge"] if len(comp.rows) > 1 else 0.0
    if samples < 2:
        raise ValueError("null test requires at least two samples for a nondegenerate distribution")
    if workers is not None and workers < 1:
        raise ValueError("workers must be positive")
    jobs = [(str(cfg.root / "lab.toml"), spec, tickers, frames, period, s) for s in range(1, samples + 1)]
    workers = workers if workers is not None else max(1, min(samples, 4, (os.cpu_count() or 2) - 1))
    if workers == 1:
        edges = [_null_worker(j) for j in jobs]
    else:
        with ProcessPoolExecutor(workers) as pool:
            edges = list(pool.map(_null_worker, jobs))
    beat = sum(e >= observed_edge for e in edges)
    s = pd.Series(edges)
    return {
        "observed_sharpe_edge": observed_edge,
        "samples": samples,
        "p_value": (1 + beat) / (1 + samples),  # conservative: counts the observation itself
        "null_mean": float(s.mean()),
        "null_std": float(s.std(ddof=1)) if samples > 1 else 0.0,
        "null_p95": float(s.quantile(0.95)),
        "null_edges": [round(e, 4) for e in edges],
    }


# ------------------------------------------------------------------------- holdout ledger


def spec_fingerprint(spec: SignalSpec) -> str:
    h = hashlib.sha256(json.dumps({"name": spec.name, "params": spec.params}, sort_keys=True).encode())
    if spec.path:
        h.update(Path(spec.path).read_bytes())
    return h.hexdigest()[:16]


class HoldoutUsed(Exception):
    pass


def holdout_ledger(cfg: LabConfig) -> list:
    p = cfg.results_dir / "holdout_ledger.json"
    return json.loads(p.read_text()) if p.exists() else []


def record_holdout(cfg: LabConfig, spec: SignalSpec, comparison: Comparison, reason: str = "") -> None:
    ledger = holdout_ledger(cfg)
    ledger.append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "signal": spec.name,
            "params": spec.params,
            "fingerprint": spec_fingerprint(spec),
            "reason_for_repeat": reason,
            "tickers": comparison.tickers,
            "rows": comparison.rows,
        }
    )
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    (cfg.results_dir / "holdout_ledger.json").write_text(json.dumps(ledger, indent=2, default=str))


def check_holdout_allowed(cfg: LabConfig, spec: SignalSpec, reason: str = "") -> None:
    fp = spec_fingerprint(spec)
    prior = [e for e in holdout_ledger(cfg) if e["fingerprint"] == fp]
    if prior and not reason:
        raise HoldoutUsed(
            f"{spec.name} with these parameters already used the holdout on {prior[0]['at']}. "
            'Re-running it turns the holdout into a test set. If you must, pass --again "reason" '
            "(the reason is recorded in the ledger)."
        )


def save_run(cfg: LabConfig, name: str, comparison: Comparison, results: list, extra: dict | None = None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = cfg.results_dir / f"{stamp}-{name}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(
            {
                "comparison": asdict(comparison),
                "extra": extra or {},
                "config": cfg.snapshot(),
                "data": {t: data.manifest(cfg).get(t) for t in comparison.tickers},
            },
            indent=2,
            default=str,
        )
    )
    curves = pd.DataFrame({r.label: r.equity for r in results})
    curves.to_csv(out / "equity.csv")
    comparison.table().to_csv(out / "table.csv", index=False)
    from lab.report import write_report

    write_report(out / "report.html", comparison, results, extra)
    return out


def today() -> date:
    return datetime.now(UTC).date()
