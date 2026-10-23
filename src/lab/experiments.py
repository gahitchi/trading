"""
Scientific procedures built on the backtester:

- compare():    each signal as an equal-weight portfolio vs the buy-and-hold control
- null_test():  the noise floor. Repeats the comparison on block-bootstrapped copies
                of the real data (real return distribution, no long-range pattern),
                drawn jointly so the stocks keep moving together, and reports how often pure noise produced an edge at least as large
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
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from lab import backtest, data, intraday, stats
from lab.config import LabConfig, Period, RiskSettings
from lab.signals.registry import SignalSpec

BASELINE = SignalSpec("buy_and_hold")


def effective_risk(cfg: LabConfig, n_tickers: int) -> RiskSettings:
    """Named profiles are tested with their actual operating limits.

    Custom keeps the legacy research convention of removing the per-name cap and
    drawdown breaker. Its explicitly configured signed exposure limits still apply:
    recreating RiskSettings with defaults would silently turn every short strategy
    into a long-only one.
    """
    del n_tickers  # kept for API stability
    if cfg.risk.profile != "custom":
        return cfg.risk
    return replace(cfg.risk, max_weight=1.0, max_drawdown=0.9999)


def run_spec(
    cfg: LabConfig,
    spec: SignalSpec,
    tickers: list[str],
    frames: dict,
    period: Period,
    label: str | None = None,
    speed: str = "daily",
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
        speed=speed,
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


def config_fingerprint(cfg: LabConfig, n_tickers: int = 1) -> str:
    """Identity of every setting that changes a backtest's result (G007-F8): the effective research risk
    limits, margin and financing, portfolio construction, costs, capital, warm-up, and the identity of the
    point-in-time universe and borrow history files. Paths, paper and research-gate settings are excluded."""

    def file_digest(p) -> str | None:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest() if p and Path(p).exists() else None

    ident = {
        "risk": asdict(effective_risk(cfg, n_tickers)),
        "margin": asdict(cfg.margin),
        "portfolio": asdict(cfg.portfolio),
        "costs": asdict(cfg.costs),
        "capital": cfg.capital,
        "warmup_start": str(cfg.warmup_start),
        "membership": file_digest(data.membership_path(cfg)),
        "borrow": file_digest(cfg.borrow.history),
        "borrow_max_age_days": cfg.borrow.max_age_days,
        "trading": asdict(cfg.trading),
    }
    return hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode()).hexdigest()[:16]


def register_trial(cfg: LabConfig, key: str, spec: SignalSpec, label: str, active: pd.Series) -> None:
    """Every configuration ever evaluated on a dataset is a trial; the Deflated Sharpe Ratio charges for all
    of them. A trial is the strategy (code + parameters) AND the settings that shape its result, so trying
    the same strategy under another risk or cost setting counts again. Re-running an identical one does not."""
    sr, *_ = stats.moments(active.to_numpy())
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    with _trials_path(cfg).open("a") as f:
        f.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "dataset": key,
                    "fingerprint": f"{spec_fingerprint(spec)}:{config_fingerprint(cfg)}",
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
    holdout_ok: bool = False,
    speed: str = "daily",
) -> tuple[Comparison, list[backtest.RunResult]]:
    sp = intraday.speed(speed)
    cfg = replace(cfg, trading=replace(cfg.trading, speed=speed))
    if sp.daily:
        guard_period(cfg, period, holdout_ok=holdout_ok)
        frames = frames or data.load_many(cfg, tickers)
        control_cfg = cfg
    else:
        frames = frames or intraday.load_many(cfg, tickers, sp)
        guard_intraday(cfg, period, frames, holdout_ok=holdout_ok)
        # The control really holds: buy-and-hold at the same speed, through the nights (no flat-at-close).
        control_cfg = replace(cfg, trading=replace(cfg.trading, flat_at_close=False))
    results = [run_spec(control_cfg, BASELINE, tickers, frames, period, "buy_and_hold (control)", speed)]
    cand_specs = [s for s in specs if s != BASELINE]
    results += [run_spec(cfg, s, tickers, frames, period, speed=speed) for s in cand_specs]
    end = period.end or max(f.index[-1] for f in frames.values()).date()
    key = dataset_key(period, str(period.start), str(end), tickers)
    if not sp.daily:
        key = f"{sp.name}|{key}"  # intraday trials are their own family

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
        m["borrow_model"] = r.borrow_model
        m["borrow_fingerprint"] = r.borrow_fingerprint
        m["borrow_fees"] = r.borrow_fees
        m["financing"] = r.financing
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
    quality = data.quality(cfg, tickers)
    if not sp.daily:
        quality = {**quality, "speed": sp.name, "intraday_history": intraday.history_note(frames, sp)}
    return Comparison(period.name, str(period.start), str(end), tickers, rows, quality), results


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
    labels = [r.label for r in results[1:]]
    if len(labels) != len(set(labels)):
        raise ValueError("candidate labels must be unique; duplicate strategies collapse matrix columns")
    cols = {r.label: active_returns(r, results[0]) for r in results[1:]}
    return pd.DataFrame(cols).dropna()


def pbo_study(
    cfg: LabConfig, specs: list[SignalSpec], tickers: list[str], period: Period, frames: dict, splits: int = 10
) -> dict:
    guard_period(cfg, period)
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
    guard_period(cfg, Period("walk-forward", start, end))
    specs = [s for s in specs if s.name != BASELINE.name]
    if not specs:
        raise ValueError("walk-forward requires at least one non-baseline candidate")
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


NULL_MAX_FALLBACK = 0.25


def _null_worker(args):
    cfg, spec, tickers, frames, period, seed = args
    # Resample only data available by the end of the evaluated period (G007-F6: drawing donors from the
    # whole file let later test/holdout rows shape the training noise floor). Warm-up history before the
    # period start is past data and stays in the donor pool.
    end = pd.Timestamp(period.end) if period.end else None
    past = {t: frames[t] if end is None else frames[t][frames[t].index <= end] for t in tickers}
    # One synchronized draw for the whole panel keeps the stocks' co-movement (ChatGPT, G002).
    boot, diag = data.block_bootstrap_panel(past, seed, return_diagnostics=True)
    cand = run_spec(cfg, spec, tickers, boot, period).metrics
    base = run_spec(cfg, BASELINE, tickers, boot, period).metrics
    worst = max((v["fallback_fraction"] for v in diag["per_instrument"].values()), default=0.0)
    return cand.get("sharpe", 0) - base.get("sharpe", 0), diag["fallback_fraction"], worst


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
    guard_period(cfg, period)
    if cfg.borrow.history is not None and cfg.risk.max_short_gross > 0:
        raise data.DataError(
            "historical short-borrow null test is not qualified: prices and borrow availability/fees "
            "need a jointly validated sampler; refusing to attach the original loan book to shuffled prices"
        )
    frames = frames or data.load_many(cfg, tickers)
    if observed_edge is None:
        comp, _ = compare(cfg, [spec], tickers, period, frames)
        observed_edge = comp.rows[1]["sharpe_edge"] if len(comp.rows) > 1 else 0.0
    jobs = [(cfg, spec, tickers, frames, period, s) for s in range(1, samples + 1)]
    workers = workers or max(1, min(samples, (os.cpu_count() or 2) - 1))
    if workers == 1:
        measured = [_null_worker(j) for j in jobs]
    else:
        # spawn, never fork: forking a process whose NautilusTrader runtime already started native threads can
        # deadlock (Python 3.13's Linux default is fork; it hung CI's offline example).
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            measured = list(pool.map(_null_worker, jobs))
    edges = [m[0] for m in measured]
    pooled = [m[1] for m in measured]
    # Where a stock has no data at the shared donor dates it falls back to its own history, losing its
    # co-movement for that block. Too much of that and the null no longer describes a synchronized market.
    # The limit is provisional, not a calibrated type-I error rate (ChatGPT, G002).
    if max(pooled) > NULL_MAX_FALLBACK:
        raise data.DataError(
            f"null test not qualified: {max(pooled):.1%} of blocks fell back to per-stock history "
            f"(provisional limit {NULL_MAX_FALLBACK:.0%})"
        )
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
        "fallback_fraction_mean": float(np.mean(pooled)),
        "fallback_fraction_max": max(pooled),
        # The pooled share can stay low while one late listing loses most of its co-movement.
        "fallback_fraction_worst_stock": max(m[2] for m in measured),
        "fallback_fraction_limit": NULL_MAX_FALLBACK,
    }


# ------------------------------------------------------------------------- holdout ledger


def spec_fingerprint(spec: SignalSpec) -> str:
    h = hashlib.sha256(json.dumps({"name": spec.name, "params": spec.params}, sort_keys=True).encode())
    if spec.path:
        h.update(Path(spec.path).read_bytes())
    return h.hexdigest()[:16]


class HoldoutUsed(Exception):
    pass


def overlaps_holdout(cfg: LabConfig, start: date, end: date | None) -> bool:
    h = cfg.period("holdout")
    return start <= (h.end or date.max) and (end or date.max) >= h.start


def guard_period(cfg: LabConfig, period: Period, *, holdout_ok: bool = False) -> None:
    """No experiment touches the holdout except `lab holdout`, which records the use (G007-F7: the guard
    used to check the period's NAME, so explicit dates overlapping the holdout passed as 'custom')."""
    if not holdout_ok and overlaps_holdout(cfg, period.start, period.end):
        h = cfg.period("holdout")
        raise HoldoutUsed(
            f"{period.name} {period.start}..{period.end or 'latest'} overlaps the holdout "
            f"({h.start}..{h.end or 'latest'}); only `lab holdout` may use it, and it records every use"
        )


def intraday_split(cfg: LabConfig, frames: dict) -> tuple[Period, Period]:
    """(research, holdout) for intraday bars: the holdout is the last `[trading] intraday_holdout` share of
    the sessions stored for every stock (at least one session).

    Free intraday history (Yahoo: ~60 days of 15m/5m, ~2 years of 1h) lies entirely inside the daily holdout,
    so intraday research cannot use the daily split: it reserves its own most recent sessions instead. The
    initial boundary is persisted across speeds and universes. Fetching more data must never turn a
    previously reserved session into training. Prior holdout ledger entries are respected on migration.
    A new prospective research protocol is required to release more training data."""
    sessions = None
    for f in frames.values():
        days = set(pd.DatetimeIndex(f.index).normalize())
        sessions = days if sessions is None else sessions & days
    sessions = sorted(sessions or [])
    if len(sessions) < 2:
        raise ValueError(f"intraday research needs at least 2 sessions stored for every stock (have {len(sessions)})")
    k = max(1, int(np.ceil(len(sessions) * cfg.trading.intraday_holdout)))
    k = min(k, len(sessions) - 1)
    boundary = sessions[-k].date()
    path = cfg.results_dir / "intraday_partition.json"
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    prior = [date.fromisoformat(e["period"][0]) for e in holdout_ledger(cfg) if e.get("speed", "daily") != "daily"]
    if prior:
        boundary = min(boundary, *prior)
    if not path.exists():
        try:
            with path.open("x") as f:
                json.dump({"version": 1, "holdout_start": str(boundary)}, f)
        except FileExistsError:
            pass  # another process reserved it; read that boundary below
    try:
        record = json.loads(path.read_text())
        if record["version"] != 1:
            raise ValueError("unknown partition version")
        recorded = date.fromisoformat(record["holdout_start"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HoldoutUsed(f"unreadable intraday partition; restore its recorded boundary: {exc}") from exc
    explicit = cfg.trading.intraday_holdout_start
    # The operator may set the boundary deliberately (e.g. when the partition was first made from a speed with
    # longer history and leaves another speed no research sessions). Sessions never run as a holdout can be
    # released; those an intraday holdout run has seen cannot (the ledger bounds every choice).
    boundary = min(date.fromisoformat(explicit) if explicit else recorded, *(prior or [date.max]))
    research_days = [s for s in sessions if s.date() < boundary]
    if not research_days:
        raise HoldoutUsed(
            f"all available intraday sessions are reserved holdout data from {boundary} "
            f"({path.relative_to(cfg.root) if path.is_relative_to(cfg.root) else path}); to choose the boundary "
            "deliberately, set [trading] intraday_holdout_start (never after a holdout already run)"
        )
    research = Period("intraday", research_days[0].date(), research_days[-1].date())
    holdout = Period("intraday_holdout", boundary, None)
    return research, holdout


def guard_intraday(cfg: LabConfig, period: Period, frames: dict, *, holdout_ok: bool = False) -> None:
    _, h = intraday_split(cfg, frames)
    if not holdout_ok and (period.end or date.max) >= h.start:
        raise HoldoutUsed(
            f"{period.name} {period.start}..{period.end or 'latest'} overlaps the intraday holdout ({h.start}.."
            "latest); only `lab holdout --speed ...` may use it, and it records every use"
        )


def holdout_ledger(cfg: LabConfig) -> list:
    p = cfg.results_dir / "holdout_ledger.json"
    return json.loads(p.read_text()) if p.exists() else []


def record_holdout(
    cfg: LabConfig, spec: SignalSpec, comparison: Comparison, reason: str = "", speed: str = "daily"
) -> None:
    ledger = holdout_ledger(cfg)
    ledger.append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "signal": spec.name,
            "params": spec.params,
            "fingerprint": spec_fingerprint(spec),
            "reason_for_repeat": reason,
            "speed": speed,
            "period": [comparison.start, comparison.end],
            "tickers": comparison.tickers,
            "rows": comparison.rows,
        }
    )
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    (cfg.results_dir / "holdout_ledger.json").write_text(json.dumps(ledger, indent=2, default=str))


def check_holdout_allowed(cfg: LabConfig, spec: SignalSpec, reason: str = "", speed: str = "daily") -> None:
    fp = spec_fingerprint(spec)
    prior = [e for e in holdout_ledger(cfg) if e["fingerprint"] == fp and e.get("speed", "daily") == speed]
    if prior and not reason:
        raise HoldoutUsed(
            f"{spec.name} with these parameters already used the holdout on {prior[0]['at']}. "
            'Re-running it turns the holdout into a test set. If you must, pass --again "reason" '
            "(the reason is recorded in the ledger)."
        )


def save_run(cfg: LabConfig, name: str, comparison: Comparison, results: list, extra: dict | None = None) -> Path:
    import uuid

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = cfg.results_dir / f"{stamp}-{uuid.uuid4().hex[:12]}-{name}"
    out.mkdir(parents=True, exist_ok=False)
    (out / "summary.json").write_text(
        json.dumps(
            {
                "comparison": asdict(comparison),
                "extra": extra or {},
                "config": cfg.snapshot(),
                "data": {t: data.manifest(cfg).get(t) for t in comparison.tickers},
                "borrow": [
                    {
                        "label": r.label,
                        "model": r.borrow_model,
                        "fingerprint": r.borrow_fingerprint,
                        "fees": r.borrow_fees,
                        "evidence": r.borrow_evidence,
                    }
                    for r in results
                ],
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
