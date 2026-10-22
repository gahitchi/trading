"""
Scientific procedures built on the backtester:

- compare():    each signal as an equal-weight portfolio vs the buy-and-hold control
- null_test():  the noise floor. Repeats the comparison on block-bootstrapped copies
                of the real data (real return distribution, no long-range pattern)
                and reports how often pure noise produced an edge at least as large
                as the real one (a p-value)
- holdout ledger: the holdout period may be used once per hypothesis; every use is
                recorded, and repeats need an explicit, logged reason
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from lab import backtest, data
from lab.config import LabConfig, Period, RiskSettings
from lab.signals.registry import SignalSpec

BASELINE = SignalSpec("buy_and_hold")


def effective_risk(cfg: LabConfig, n_tickers: int) -> RiskSettings:
    """Risk settings for research runs.

    - An equal-weight portfolio of n stocks needs 1/n per stock, or the risk agent
      would trim the control itself: the cap is raised to 1/n if necessary.
    - The drawdown circuit breaker is OFF in research: once tripped it would turn
      the buy-and-hold control into "buy-and-hold until the first crash, then cash",
      and every comparison would measure the breaker, not the signal. It is always
      on in paper trading.
    """
    r = cfg.risk
    return RiskSettings(max(r.max_weight, 1.0 / n_tickers), r.max_gross, 0.9999)


def run_spec(cfg: LabConfig, spec: SignalSpec, tickers: list[str], frames: dict, period: Period,
             label: str | None = None) -> backtest.RunResult:
    w = 1.0 / len(tickers)
    sleeves = [backtest.Sleeve(spec.make(), t, w) for t in tickers]
    return backtest.run(cfg, sleeves, frames, period.start, period.end, label=label or sleeves[0].signal.label(),
                        risk=effective_risk(cfg, len(tickers)))


@dataclass
class Comparison:
    period: str
    start: str
    end: str
    tickers: list
    rows: list  # dicts: label, metrics..., sharpe_edge, return_edge

    def table(self) -> pd.DataFrame:
        cols = ["label", "total_return", "cagr", "sharpe", "max_drawdown", "avg_exposure", "fills", "commissions",
                "sharpe_edge"]
        df = pd.DataFrame(self.rows)
        return df[[c for c in cols if c in df.columns]]


def compare(cfg: LabConfig, specs: list[SignalSpec], tickers: list[str], period: Period,
            frames: dict | None = None) -> tuple[Comparison, list[backtest.RunResult]]:
    frames = frames or data.load_many(cfg, tickers)
    results = [run_spec(cfg, BASELINE, tickers, frames, period, "buy_and_hold (control)")]
    results += [run_spec(cfg, s, tickers, frames, period) for s in specs if s != BASELINE]
    base = results[0].metrics
    rows = []
    for r in results:
        m = dict(r.metrics)
        m["label"] = r.label
        m["sharpe_edge"] = m.get("sharpe", 0) - base.get("sharpe", 0)
        m["return_edge"] = m.get("total_return", 0) - base.get("total_return", 0)
        m["signal_errors"] = r.signal_errors
        m["rejections"] = r.rejections
        m["risk_events"] = [e.kind for e in r.risk_events]
        rows.append(m)
    end = period.end or max(f.index[-1] for f in frames.values()).date()
    return Comparison(period.name, str(period.start), str(end), tickers, rows), results


# ------------------------------------------------------------------------- noise floor


def _null_worker(args):
    cfg_path, spec, tickers, frames, period, seed = args
    from lab.config import load_config

    cfg = load_config(cfg_path)
    boot = {t: data.block_bootstrap(frames[t], seed * 1000 + i) for i, t in enumerate(tickers)}
    cand = run_spec(cfg, spec, tickers, boot, period).metrics
    base = run_spec(cfg, BASELINE, tickers, boot, period).metrics
    return cand.get("sharpe", 0) - base.get("sharpe", 0)


def null_test(cfg: LabConfig, spec: SignalSpec, tickers: list[str], period: Period, samples: int,
              frames: dict | None = None, workers: int | None = None, observed_edge: float | None = None) -> dict:
    frames = frames or data.load_many(cfg, tickers)
    if observed_edge is None:
        comp, _ = compare(cfg, [spec], tickers, period, frames)
        observed_edge = comp.rows[1]["sharpe_edge"] if len(comp.rows) > 1 else 0.0
    jobs = [(str(cfg.root / "lab.toml"), spec, tickers, frames, period, s) for s in range(1, samples + 1)]
    workers = workers or max(1, min(samples, (os.cpu_count() or 2) - 1))
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
    ledger.append({
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "signal": spec.name, "params": spec.params, "fingerprint": spec_fingerprint(spec),
        "reason_for_repeat": reason, "tickers": comparison.tickers, "rows": comparison.rows,
    })
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    (cfg.results_dir / "holdout_ledger.json").write_text(json.dumps(ledger, indent=2, default=str))


def check_holdout_allowed(cfg: LabConfig, spec: SignalSpec, reason: str = "") -> None:
    fp = spec_fingerprint(spec)
    prior = [e for e in holdout_ledger(cfg) if e["fingerprint"] == fp]
    if prior and not reason:
        raise HoldoutUsed(f"{spec.name} with these parameters already used the holdout on {prior[0]['at']}. "
                          "Re-running it turns the holdout into a test set. If you must, pass --again \"reason\" "
                          "(the reason is recorded in the ledger).")


def save_run(cfg: LabConfig, name: str, comparison: Comparison, results: list, extra: dict | None = None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = cfg.results_dir / f"{stamp}-{name}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps({
        "comparison": asdict(comparison), "extra": extra or {}, "config": cfg.snapshot(),
        "data": {t: data.manifest(cfg).get(t) for t in comparison.tickers},
    }, indent=2, default=str))
    curves = pd.DataFrame({r.label: r.equity for r in results})
    curves.to_csv(out / "equity.csv")
    comparison.table().to_csv(out / "table.csv", index=False)
    return out


def today() -> date:
    return datetime.now(UTC).date()
