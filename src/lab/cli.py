"""lab: command-line interface for the trading lab."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime

import pandas as pd

from lab import data, experiments
from lab.config import ConfigError, LabConfig, Period, load_config
from lab.market_analysis import MarketAnalysisError
from lab.multiasset import MultiAssetError
from lab.signals.builtin import BUILTIN
from lab.signals.registry import SignalNotFound, available_research, resolve


def _period(cfg: LabConfig, a) -> Period:
    if a.period == "holdout":
        raise SystemExit("the holdout is guarded: use `lab holdout <signal>`")
    if a.start or a.end:
        start = date.fromisoformat(a.start) if a.start else cfg.period(a.period).start
        end = date.fromisoformat(a.end) if a.end else None
        holdout_start = cfg.period("holdout").start
        if end is None or end >= holdout_start:
            raise SystemExit("custom research dates may not overlap the sealed holdout or be open-ended")
        return Period("custom", start, end)
    return cfg.period(a.period)


def _params(sets: list[str]) -> dict[str, dict]:
    """--set sma_trend.fast=20 -> {"sma_trend": {"fast": 20}}"""
    out: dict[str, dict] = {}
    for s in sets or []:
        key, _, val = s.partition("=")
        name, _, param = key.partition(".")
        if not (name and param and val):
            raise SystemExit(f"bad --set {s!r}; use signal.param=value")
        try:
            v = json.loads(val)
        except json.JSONDecodeError:
            v = val
        out.setdefault(name, {})[param] = v
    return out


def _tickers(cfg: LabConfig, given: list[str] | None) -> list[str]:
    if given:
        tickers = [t.upper() for t in given]
        missing = [t for t in tickers if t not in data.manifest(cfg)]
        if missing:
            raise SystemExit(f"no data for {missing}; run: lab data fetch {' '.join(missing)}")
        return tickers
    tickers, missing = experiments.research_tickers(cfg)
    if data.membership(cfg) is not None:
        if missing:
            print(
                f"note: {len(missing)} of {len(tickers) + len(missing)} universe members have no data and are left "
                f"out ({', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}); that reintroduces survivorship bias"
            )
    elif missing:
        raise SystemExit(f"no data for {missing}; run: lab data fetch {' '.join(missing)}")
    if not tickers:
        raise SystemExit("no data for any universe ticker; run: lab data fetch")
    return tickers


def _print_table(comp: experiments.Comparison) -> None:
    df = comp.table().copy()
    for c in ("total_return", "cagr", "max_drawdown", "avg_exposure"):
        if c in df:
            df[c] = df[c].map(lambda v, c=c: f"{v:+.1%}" if c in ("total_return", "cagr") else f"{v:.0%}")
    if "dsr" in df:
        df["dsr"] = df["dsr"].map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
    for c in ("sharpe", "sharpe_edge"):
        if c in df:
            df[c] = df[c].map(lambda v, c=c: f"{v:+.2f}" if c == "sharpe_edge" else f"{v:.2f}")
    shown = ", ".join(comp.tickers[:12]) + (" ..." if len(comp.tickers) > 12 else "")
    print(f"\n{comp.period} {comp.start} .. {comp.end} | {len(comp.tickers)} stocks: {shown}")
    print(df.to_string(index=False))
    print(
        "sharpe_edge: Sharpe minus buy-and-hold's. dsr: Deflated Sharpe Ratio of the daily return difference, "
        "counting every configuration tried on this data (>= 0.95 is significant)."
    )
    warning = data.quality_warning(comp.data_quality) if comp.data_quality else None
    if warning:
        print(f"DATA QUALITY: {warning}")


# ----------------------------------------------------------------------------- data


def cmd_data_fetch(cfg, a):
    tickers = [t.upper() for t in a.tickers] or list(cfg.universe)
    start = date.fromisoformat(a.start) if a.start else cfg.warmup_start
    failed = []
    for t in tickers:
        try:
            df = data.fetch_yfinance(t, start)
            e = data.save(cfg, t, df, source="yahoo_finance", adjusted=True, survivorship_free=False)
            print(f"{t:<6} {e['rows']:>5} days  {e['first']} .. {e['last']}")
        except data.DataError as err:
            failed.append(t)
            print(f"{t:<6} FAILED: {err}")
    return 1 if failed else 0


def cmd_data_import(cfg, a):
    df = data.import_csv(a.file, a.date_column)
    e = data.save(
        cfg,
        a.ticker,
        df,
        source=a.source or f"csv:{a.file}",
        adjusted=a.adjusted,
        survivorship_free=a.survivorship_free,
        note=a.note or "",
    )
    print(f"{a.ticker.upper()}: {e['rows']} days {e['first']} .. {e['last']}")


def cmd_data_membership(cfg, a):
    df = data.save_membership(cfg, a.file)
    print(
        f"point-in-time universe: {df.ticker.nunique()} tickers, {len(df)} membership spells "
        f"-> {data.membership_path(cfg).relative_to(cfg.root)}"
    )
    have = data.manifest(cfg)
    missing = sorted(set(df.ticker) - set(have))
    if missing:
        print(f"no price data yet for {len(missing)}: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")


def cmd_data_sample(cfg, a):
    e = data.save(
        cfg,
        "GOOG",
        data.sample_goog(),
        source="backtesting.py sample (real GOOG prices)",
        adjusted=False,
        note="unadjusted; for offline pipeline checks",
    )
    print(f"GOOG: {e['rows']} days {e['first']} .. {e['last']} (real prices, offline sample)")


def cmd_data_list(cfg, a):
    m = data.manifest(cfg)
    if not m:
        print("no data yet: lab data fetch")
        return
    for t, e in sorted(m.items()):
        print(
            f"{t:<6} {e['rows']:>5} days  {e['first']} .. {e['last']}  {e['source']}{'' if e['adjusted'] else ' (unadjusted)'}"
        )


# ----------------------------------------------------------------------------- descriptive market analytics


def cmd_analyze(cfg, a):
    """Read-only, historical analysis; sealed holdout is never accessible here."""
    from dataclasses import asdict
    from datetime import timedelta
    from pathlib import Path

    from lab.market_analysis import AnalysisSpec, MarketAnalysisError, analyze_market

    tickers = _tickers(cfg, a.tickers)
    history = data.load_many_unmodified(cfg, tickers)
    max_research_date = cfg.period("holdout").start - timedelta(days=1)
    if a.as_of:
        try:
            as_of = date.fromisoformat(a.as_of)
        except ValueError as exc:
            raise MarketAnalysisError("--as-of must be an ISO YYYY-MM-DD exchange-session date") from exc
        if as_of > max_research_date:
            raise MarketAnalysisError(
                f"analysis cutoff {as_of} overlaps the sealed holdout; last permissible research date "
                f"is {max_research_date}"
            )
    else:
        as_of = min(max_research_date, *(f.index[-1].date() for f in history.values()))
    spec = AnalysisSpec(
        lookback=a.lookback,
        risk_window=a.risk_window,
        momentum_days=a.momentum_days,
        skip_recent=a.skip_recent,
        covariance_shrinkage=a.shrinkage,
    )
    result = analyze_market(history, as_of=as_of, spec=spec, provenance=data.manifest(cfg))
    issues = data.quality_warning(data.quality(cfg, tickers))
    if issues:
        result["warnings"].append("Data quality: " + issues)
    result["spec"] = asdict(spec)
    if a.output:
        output = Path(a.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
        except FileExistsError as exc:
            raise MarketAnalysisError(f"output exists; refusing to overwrite {output}") from exc
    if a.json:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return
    market = result["market"]
    print(f"Market analysis {result['as_of']} | {len(tickers)} instruments | {result['qualification']}")
    print(
        f"  21-day positive breadth {market['breadth_positive_21d']:.1%}; "
        f"12-1m positive breadth {market['breadth_positive_skip_recent']:.1%}"
    )
    print(f"  Descriptive market state: {market['descriptive_regime']}")
    print(f"  Median annualized realized volatility: {market['median_asset_vol_annual']:.1%}")
    for ticker, values in result["features"].items():
        print(
            f"  {ticker}: 12-1m {values['momentum_skip_recent']:+.1%}; "
            f"realized vol {values['realized_vol_annual']:.1%}; "
            f"historical ES {values['historical_expected_shortfall_loss']:.1%}; "
            f"median dollar volume ${values['median_dollar_volume']:,.0f}"
        )
    print(f"  Deterministic analysis ID: {result['analysis_id']}")
    for warning in result["warnings"]:
        print(f"  WARNING: {warning}")
    if a.output:
        print(f"  Saved JSON: {a.output}")


# ----------------------------------------------------------------------------- exploratory probabilistic research


def cmd_forecast(cfg, a):
    """Read-only historical forecast, strictly outside the protected holdout."""
    from datetime import timedelta
    from pathlib import Path

    from lab.predictive_research import ForecastSpec, forecast_research

    ticker = _tickers(cfg, [a.ticker])[0]
    history = data.load_many_unmodified(cfg, [ticker])[ticker]
    limit = cfg.period("holdout").start - timedelta(days=1)
    if a.as_of:
        try:
            as_of = date.fromisoformat(a.as_of)
        except ValueError as exc:
            raise MarketAnalysisError("--as-of requires a YYYY-MM-DD exchange-session date") from exc
        if as_of > limit:
            raise MarketAnalysisError(f"forecast {as_of} overlaps the sealed holdout; cutoff must be <= {limit}")
    else:
        dates = history.index[history.index <= pd.Timestamp(limit)]
        if dates.empty:
            raise MarketAnalysisError("no historical bars before the sealed holdout")
        as_of = dates[-1].date()
    spec = ForecastSpec(
        horizon=a.horizon,
        min_train=a.min_train,
        calibration=a.calibration,
        test=a.test_sessions,
        ridge_alpha=a.ridge_alpha,
        coverage=a.coverage,
        drift_z_limit=a.drift_z_limit,
    )
    report = forecast_research(ticker, history, as_of=as_of, spec=spec, provenance=data.manifest(cfg).get(ticker))
    warning = data.quality_warning(data.quality(cfg, [ticker]))
    if warning:
        report["warnings"].append("Data quality: " + warning)
    if a.output:
        output = Path(a.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("x", encoding="utf-8") as file:
                file.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
        except FileExistsError as exc:
            raise MarketAnalysisError(f"output exists; refusing to overwrite {output}") from exc
    if a.json:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return
    forecast = report["as_of_forecast"]
    oos = report["frozen_oos_test"]
    regime = report["volatility_regime"]
    print(f"{ticker} {as_of} | research-only {a.horizon}-session probabilistic forecast")
    print(
        f"  Predicted return {forecast['expected_return_fraction']:+.2%} "
        f"| exploratory interval [{forecast['lower_return_fraction']:+.2%}, "
        f"{forecast['upper_return_fraction']:+.2%}]"
    )
    print(
        f"  Estimated probability positive {forecast['empirical_probability_positive']:.1%} "
        f"| high-volatility state probability {regime['high_volatility_probability_filtered']:.1%}"
    )
    print(
        f"  Frozen OOS MAE {oos['model_mae_log_return']:.5f} "
        f"vs mean baseline {oos['train_mean_baseline_mae_log_return']:.5f}"
        f" | empirical interval coverage {oos['observed_interval_coverage']:.1%}"
    )
    print(f"  Status {report['forecast_status']} | analysis ID {report['analysis_id']}")
    print("  WARNING: Probabilities and intervals are not qualified trading signals.")
    if a.output:
        print(f"  Saved JSON: {a.output}")


# ----------------------------------------------------------------------------- offline capital-market and FX scenario research


def cmd_capital_audit(cfg, a):
    """Analyze a typed offline portfolio; no broker imports, calls or orders."""
    from pathlib import Path

    from lab.multiasset import MultiAssetError
    from lab.multiasset_report import analyze_manifest, load_manifest

    try:
        report = analyze_manifest(load_manifest(a.manifest))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MultiAssetError):
            raise
        raise MultiAssetError(f"invalid portfolio manifest: {exc}") from exc
    if a.output:
        output = Path(a.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output.open("x", encoding="utf-8") as file:
                file.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
        except FileExistsError as exc:
            raise MultiAssetError(f"output exists; refusing to overwrite {output}") from exc
    if a.json:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return
    print(f"Offline capital-markets audit | {report['as_of']} | {report['qualification']}")
    print(f"  Classes: {', '.join(report['asset_classes'])} | currency: {report['reporting_currency']}")
    print(f"  Baseline bid-side mark: {report['liquidation_mark_before']}")
    print(f"  Spot preview mark: {report['liquidation_mark_after_spot_previews']}")
    for key in ("stress_before", "stress_after_spot_previews"):
        risk = report[key]
        if risk is None:
            print(f"  {key}: NO_SCENARIO_EVIDENCE")
        else:
            print(
                f"  {key}: {risk['risk_status']} | worst case {risk['worst_case_name']}"
                f" | loss {risk['worst_case_loss']}"
            )
            for item in risk["ranked_scenarios"]:
                print(
                    f"    {item['name']}: loss {item['loss_fraction']} "
                    f"| breaches {', '.join(item['breached_limits']) or 'none'}"
                )
    print(f"  Analysis ID: {report['analysis_id']}")
    print("  WARNING: User-defined scenarios are hypotheses, not risk guarantees or trade permits.")
    if a.output:
        print(f"  Saved JSON: {a.output}")


# ----------------------------------------------------------------------------- experiments


def _specs(cfg, names, sets, require_tradable=False):
    params = _params(sets)
    try:
        return [resolve(n, cfg.research_dir, params.get(n), require_tradable=require_tradable) for n in names]
    except (SignalNotFound, ValueError) as e:
        raise SystemExit(str(e)) from e


def _with_target_vol(cfg, a):
    import dataclasses

    if getattr(a, "target_vol", None) is None:
        return cfg
    return dataclasses.replace(cfg, portfolio=dataclasses.replace(cfg.portfolio, target_vol=a.target_vol))


def cmd_backtest(cfg, a):
    if a.period == "holdout" and not (a.start or a.end):
        raise SystemExit("the holdout is guarded: use `lab holdout <signal>`")
    cfg = _with_target_vol(cfg, a)
    specs = _specs(cfg, a.signals, a.set)
    tickers = _tickers(cfg, a.tickers)
    comp, results = experiments.compare(cfg, specs, tickers, _period(cfg, a))
    _print_table(comp)
    for r in results:
        if r.signal_errors or r.rejections:
            print(f"  note: {r.label}: {r.signal_errors} signal errors, {r.rejections} rejected orders")
    out = experiments.save_run(cfg, "backtest", comp, results)
    print(f"\nsaved: {out.relative_to(cfg.root)}  (report: {(out / 'report.html').relative_to(cfg.root)})")


def cmd_null(cfg, a):
    (spec,) = _specs(cfg, [a.signal], a.set)
    tickers = _tickers(cfg, a.tickers)
    period = _period(cfg, a)
    if period.name == "holdout":
        raise SystemExit("the holdout is guarded: use `lab holdout <signal>`")
    frames = data.load_many(cfg, tickers)
    comp, results = experiments.compare(cfg, [spec], tickers, period, frames)
    _print_table(comp)
    print(f"\nnoise floor: {a.samples} block-bootstrap resamples of the real data...")
    nt = experiments.null_test(
        cfg, spec, tickers, period, a.samples, frames, workers=a.workers, observed_edge=comp.rows[1]["sharpe_edge"]
    )
    print(
        f"observed Sharpe edge {nt['observed_sharpe_edge']:+.2f} | noise: mean {nt['null_mean']:+.2f}, "
        f"sd {nt['null_std']:.2f}, 95th pct {nt['null_p95']:+.2f} | p = {nt['p_value']:.3f}"
    )
    verdict = "beats the noise floor" if nt["p_value"] <= cfg.research.max_p_value else "indistinguishable from noise"
    print(f"=> {verdict} (threshold p <= {cfg.research.max_p_value})")
    out = experiments.save_run(cfg, f"null-{spec.name}", comp, results, {"null_test": nt})
    print(f"saved: {out.relative_to(cfg.root)}")


def _grid(items: list[str]) -> dict[str, list]:
    """fast=20,50 slow=100,200 -> {"fast": [20, 50], "slow": [100, 200]}"""
    out = {}
    for it in items:
        k, _, vals = it.partition("=")
        if not vals:
            raise SystemExit(f"bad --grid item {it!r}; use param=v1,v2,...")
        out[k] = [json.loads(v) if v.replace(".", "", 1).lstrip("-").isdigit() else v for v in vals.split(",")]
    return out


def _grid_specs(cfg, a):
    try:
        return experiments.grid_specs(a.signal, _grid(a.grid), cfg.research_dir)
    except (SignalNotFound, ValueError) as e:
        raise SystemExit(str(e)) from e


def cmd_walk_forward(cfg, a):
    specs = _grid_specs(cfg, a)
    tickers = _tickers(cfg, a.tickers)
    start = date.fromisoformat(a.start) if a.start else cfg.period("train").start
    end = date.fromisoformat(a.end) if a.end else cfg.period("train").end
    if end >= cfg.period("holdout").start:
        raise SystemExit("walk-forward may not reach into the holdout period")
    print(
        f"walk-forward: {len(specs)} configurations, {a.is_years}y in-sample -> {a.oos_years}y out-of-sample, "
        f"{start} .. {end}"
    )
    w = experiments.walk_forward(cfg, specs, tickers, start, end, data.load_many(cfg, tickers), a.is_years, a.oos_years)
    for s in w["windows"]:
        print(
            f"  IS {s['in_sample']}  chose {s['chosen']:<40} IS active Sharpe {s['is_active_sharpe']:+.2f} | "
            f"OOS {s['out_of_sample']} active Sharpe {s['oos_active_sharpe']:+.2f}, "
            f"return {s['oos_return']:+.1%} vs buy-and-hold {s['oos_buy_and_hold']:+.1%}"
        )
    lo, hi = w["oos_active_sharpe_ci"]
    print(
        f"\nstitched out-of-sample active Sharpe {w['oos_active_sharpe']:+.2f} (95% CI {lo:+.2f} .. {hi:+.2f}), "
        f"PSR {w['oos_psr']:.2f}, efficiency (OOS/IS) {w['efficiency']:.2f}, "
        f"{w['distinct_choices']} distinct configuration(s) chosen"
    )
    out = cfg.results_dir / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-walkforward-{a.signal}.json"
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(w, indent=2, default=str))
    print(f"saved: {out.relative_to(cfg.root)}")


def cmd_pbo(cfg, a):
    specs = _grid_specs(cfg, a)
    tickers = _tickers(cfg, a.tickers)
    period = _period(cfg, a)
    if period.name == "holdout":
        raise SystemExit("the holdout is guarded: use `lab holdout <signal>`")
    print(f"probability of backtest overfitting: {len(specs)} configurations, {a.splits} blocks, {period.name}")
    p = experiments.pbo_study(cfg, specs, tickers, period, data.load_many(cfg, tickers), a.splits)
    for label, sr in p["active_sharpe_by_config"].items():
        print(f"  {label:<50} active Sharpe {sr:+.2f}")
    verdict = "likely overfit" if p["pbo"] > 0.5 else ("doubtful" if p["pbo"] > 0.2 else "robust to selection")
    print(
        f"\nPBO = {p['pbo']:.2f} over {p['combinations']} splits ({verdict}); in-sample winner's out-of-sample "
        f"Sharpe {p['oos_sharpe_of_is_winner_mean']:+.2f} vs in-sample {p['is_sharpe_of_is_winner_mean']:+.2f}; "
        f"P(OOS loss) {p['prob_oos_loss']:.2f}"
    )
    return 0


def cmd_holdout(cfg, a):
    (spec,) = _specs(cfg, [a.signal], a.set)
    tickers = _tickers(cfg, a.tickers)
    try:
        experiments.check_holdout_allowed(cfg, spec, a.again or "")
    except experiments.HoldoutUsed as e:
        raise SystemExit(str(e)) from e
    comp, results = experiments.compare(cfg, [spec], tickers, cfg.period("holdout"))
    experiments.record_holdout(cfg, spec, comp, a.again or "")
    _print_table(comp)
    out = experiments.save_run(cfg, f"holdout-{spec.name}", comp, results)
    print(f"\nrecorded in results/holdout_ledger.json; saved: {out.relative_to(cfg.root)}")


# ----------------------------------------------------------------------------- research swarm


def _provider(cfg, a):
    import dataclasses

    from lab.research.providers import make_provider

    rs = cfg.research
    if getattr(a, "provider", None) or getattr(a, "model", None):
        rs = dataclasses.replace(rs, provider=a.provider or rs.provider, model=a.model or rs.model)
    return make_provider(rs, cfg.root)


def cmd_llm_check(cfg, a):
    from lab.research.providers import LLMError

    p = _provider(cfg, a)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "sum": {"type": "integer"}},
        "required": ["ok", "sum"],
        "additionalProperties": False,
    }
    try:
        out = p.complete_json(
            role="check", system="Answer exactly as asked.", prompt="Return ok=true and sum = 17 + 25.", schema=schema
        )
    except LLMError as e:
        raise SystemExit(f"{p.name}/{p.model}: FAILED: {e}") from e
    good = out == {"ok": True, "sum": 42}
    print(f"{p.name}/{p.model}: {'working' if good else 'answered, but wrong'} ({p.calls[-1].seconds}s) -> {out}")
    return 0 if good else 1


def cmd_research_run(cfg, a):
    from lab.research.orchestrator import Orchestrator
    from lab.research.providers import LLMError

    have = [t for t in cfg.universe if t in data.manifest(cfg)]
    if not have:
        raise SystemExit("no market data for the universe; run: lab data fetch")
    try:
        orch = Orchestrator(cfg, _provider(cfg, a))
    except LLMError as e:
        raise SystemExit(str(e)) from e
    print(f"research swarm: {orch.provider.name}/{orch.provider.model}, universe with data: {', '.join(have)}")
    for i in range(a.cycles):
        print(f"\n=== cycle {i + 1}/{a.cycles} ===")
        h = orch.cycle()
        if h:
            print(f"result: {h.id} -> {h.status}")
    calls = orch.provider.calls
    print(f"\nmodel calls: {len(calls)} ({sum(c.seconds for c in calls):.0f}s). Notebook: research/NOTEBOOK.md")


def cmd_research_retry(cfg, a):
    from lab.research.orchestrator import Orchestrator
    from lab.research.providers import LLMError

    try:
        orch = Orchestrator(cfg, _provider(cfg, a))
        h = orch.retry(a.id)
    except (LLMError, ValueError) as e:
        raise SystemExit(str(e)) from e
    print(f"result: {h.id} -> {h.status}")


def cmd_research_list(cfg, a):
    from lab.research.orchestrator import Hypothesis

    d = cfg.research_dir / "hypotheses"
    if not d.exists():
        print("no hypotheses yet: lab research run")
        return
    for p in sorted(d.iterdir()):
        h = Hypothesis(cfg.research_dir, p.name)
        prop = h.read("proposal.json") or {}
        t = h.read("train.json")
        extra = f"  edge {t['rows'][1]['sharpe_edge']:+.2f} p={t['null']['p_value']:.3f}" if t else ""
        print(f"{h.id:<42} {h.status or '?':<19}{extra}  {prop.get('title', '')[:60]}")


def cmd_research_show(cfg, a):
    from lab.research.orchestrator import Hypothesis

    h = Hypothesis(cfg.research_dir, a.id)
    if not h.dir.exists():
        raise SystemExit(f"no hypothesis {a.id}")
    print(json.dumps(h.read("proposal.json"), indent=2))
    print(f"\nstatus: {h.status}")
    for name in ("review.json", "train.json", "test.json"):
        obj = h.read(name)
        if obj:
            if name != "review.json":
                obj = {k: v for k, v in obj.items() if k in ("passed", "checks", "sharpe_edge", "survived")} | {
                    "candidate": obj["rows"][1]
                }
            print(f"\n{name}:\n{json.dumps(obj, indent=2, default=str)[:3000]}")
    print(f"\ncode: {h.dir / 'signal.py'}")


def cmd_research_promote(cfg, a):
    from lab.research.orchestrator import Hypothesis

    h = Hypothesis(cfg.research_dir, a.id)
    if h.status != "candidate":
        raise SystemExit(f"{a.id} is {h.status!r}; only candidates (passed train AND test) can be promoted")
    print((h.dir / "signal.py").read_text())
    if not a.yes and input("Read the code above. Promote it to paper trading? [y/N] ").strip().lower() != "y":
        raise SystemExit("not promoted")
    h.set_status("promoted", "human approval")
    print(f"{a.id} promoted; it can now be used with: lab paper --signals {a.id}")


# ----------------------------------------------------------------------------- paper


def cmd_paper(cfg, a):
    from lab.notify import Notifier
    from lab.paper import build_node, build_smoke_node
    from lab.safety import SafetyError

    tickers = [t.upper() for t in (a.tickers or cfg.universe)]
    try:
        if a.smoke:
            node, report = build_smoke_node(cfg, tickers, test_order=a.test_order)
        else:
            specs = _specs(cfg, a.signals, a.set)
            for s in specs:
                if s.path is not None:
                    resolve(s.name, cfg.research_dir, s.params, require_tradable=True)
            node = build_node(cfg, specs, tickers, adopt_broker=a.adopt_broker)
    except (SafetyError, ValueError, SignalNotFound) as e:
        raise SystemExit(str(e)) from e
    what = "smoke test" if a.smoke else f"{len(a.signals)} signal(s) x {len(tickers)} stocks"
    print(f"paper node ready: {what}, account {cfg.paper.account_id} via {cfg.paper.host}:{cfg.paper.port}")
    if a.check_only:
        node.dispose()
        print("check only: configuration is valid; not connecting.")
        return 0
    notifier = Notifier()
    if not a.smoke:
        notifier.send(f"paper trading starting: {what}")
    print("connecting to Interactive Brokers... (Ctrl+C to stop)")
    try:
        node.run()
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        node.dispose()
        notifier.flush()
    if a.smoke:
        if not report.exists():
            print("smoke test produced no report: the node did not get far enough (check the log above)")
            return 1
        r = json.loads(report.read_text())
        for name, c in r["checks"].items():
            print(f"  {'✓' if c['ok'] else '✗'} {name}: {c['detail']}")
        print(f"smoke test {'PASSED' if r['passed'] else 'FAILED'} (report: {report.relative_to(cfg.root)})")
        return 0 if r["passed"] else 1
    return 0


def cmd_paper_report(cfg, a):
    from lab.paper import runtime_dir
    from lab.paper_report import load_journal, summarize

    journals = sorted(runtime_dir(cfg).glob("journal-*.jsonl"))
    if not journals:
        raise SystemExit("no paper-trading journal yet (results/paper/journal-*.jsonl)")
    r = summarize(load_journal(journals))
    if "last_equity" in r:
        print(
            f"{r['start']} .. {r['end']}: equity {r['first_equity']:,.2f} -> {r['last_equity']:,.2f} "
            f"({r['total_return']:+.2%}), max drawdown {r['max_drawdown']:.1%}"
        )
    print(
        f"fills: {r['fills']}, traded {r.get('traded_value', 0):,.0f} USD, commissions {r.get('commissions', 0):,.2f} USD"
    )
    if "shortfall_bps" in r:
        sb = r["shortfall_bps"]
        print(
            f"implementation shortfall vs decision close: mean {sb['mean']:+.1f} bps, median {sb['median']:+.1f}, "
            f"90th pct {sb['p90']:+.1f} over {sb['fills_measured']} fills (positive = cost)"
        )
    if r["risk_events"]:
        print("risk events: " + ", ".join(f"{k} x{v}" for k, v in sorted(r["risk_events"].items())))
    return 0


def cmd_kill(cfg, a):
    from lab.notify import Notifier
    from lab.paper import kill_path
    from lab.safety import set_kill_switch

    state = set_kill_switch(kill_path(cfg), "flatten" if a.flatten else "halt", a.reason)
    msg = (
        "KILL SWITCH: no new orders; open orders are cancelled within a minute"
        + ("; all positions exit at the next open" if a.flatten else "")
        + f". Reason: {a.reason}"
    )
    print(msg)
    Notifier().send_now(msg)
    print(f"set {kill_path(cfg).relative_to(cfg.root)} at {state['at']}. Undo with: lab resume")


def cmd_resume(cfg, a):
    from lab.notify import Notifier
    from lab.paper import kill_path
    from lab.safety import clear_kill_switch

    if clear_kill_switch(kill_path(cfg)):
        print("kill switch cleared; trading resumes at the next rebalance")
        Notifier().send_now("kill switch cleared; trading resumes at the next rebalance")
    else:
        print("kill switch was not set")


def _heartbeat_age(cfg):
    from lab.paper import heartbeat_path

    p = heartbeat_path(cfg)
    if not p.exists():
        return None, None
    try:
        hb = json.loads(p.read_text(encoding="utf-8"))
        age = (datetime.now(UTC) - datetime.fromisoformat(hb["at"])).total_seconds()
        return age, hb
    except (ValueError, TypeError, KeyError, OSError):
        return None, None


def cmd_health(cfg, a):
    from lab.health import assess_heartbeat
    from lab.paper import runtime_dir
    from lab.persistence import StateError, read_risk_checkpoint

    age, hb = _heartbeat_age(cfg)
    try:
        risk_halted = read_risk_checkpoint(runtime_dir(cfg) / "risk.json")["halted"]
    except StateError:
        risk_halted = True
    issues = assess_heartbeat(hb, age, a.max_age, risk_halted=risk_halted)
    if issues:
        print(f"unhealthy: {', '.join(issues)}; heartbeat: {json.dumps(hb)}")
        return 1
    # There may be queued opening orders or a pending daily data snapshot; that
    # makes the system not ready to place *new* orders, not disconnected.
    print(f"healthy (order-ready={hb.get('ready') is True}): heartbeat age {age:.0f}s; {json.dumps(hb)}")
    return 0


def cmd_doctor(cfg, a):
    import shutil
    import socket

    import nautilus_trader

    from lab.notify import Notifier
    from lab.paper import kill_path
    from lab.safety import SafetyError, check_paper_settings, read_kill_switch

    results = []

    def check(name, status, detail):
        results.append((name, status, detail))

    check("python / nautilus", "ok", f"{sys.version.split()[0]} / nautilus_trader {nautilus_trader.__version__}")
    m = data.manifest(cfg)
    missing = [t for t in cfg.universe if t not in m]
    stale = [t for t in cfg.universe if t in m and (date.today() - date.fromisoformat(m[t]["last"])).days > 7]
    check(
        "market data",
        "fail" if missing else ("warn" if stale else "ok"),
        f"missing {missing}"
        if missing
        else (f"older than a week: {stale} (lab data fetch)" if stale else f"{len(m)} tickers up to date"),
    )
    survivorship = all(m.get(t, {}).get("survivorship_free") for t in cfg.universe if t in m)
    check(
        "data quality",
        "ok" if survivorship else "warn",
        "survivorship-free" if survivorship else "not survivorship-free: results are biased upward (see README)",
    )
    p = cfg.paper
    try:
        check_paper_settings(p.port, p.account_id)
        check("paper settings", "ok", f"{p.account_id} via {p.host}:{p.port}")
    except SafetyError as e:
        check("paper settings", "fail", str(e))
    try:
        with socket.create_connection((p.host, p.port), timeout=3):
            check("IB API reachable", "ok", f"{p.host}:{p.port} accepts connections")
    except OSError as e:
        check("IB API reachable", "fail", f"{p.host}:{p.port}: {e} (is IB Gateway running and logged in?)")
    kill = read_kill_switch(kill_path(cfg))
    check(
        "kill switch",
        "warn" if kill else "ok",
        f"SET ({kill['mode']}): {kill.get('reason', '')}" if kill else "not set",
    )
    age, _ = _heartbeat_age(cfg)
    check(
        "agents heartbeat",
        "ok" if age is not None and age < 180 else "warn",
        "no heartbeat (not running)" if age is None else f"last {age:.0f}s ago",
    )
    rs = cfg.research
    if rs.provider == "claude_code":
        check(
            "research provider",
            "ok" if shutil.which("claude") else "warn",
            "claude CLI found" if shutil.which("claude") else "claude CLI not installed (research swarm unavailable)",
        )
    else:
        try:
            with socket.create_connection(tuple(rs.ollama_url.split("//")[-1].split(":")), timeout=3):
                check("research provider", "ok", f"Ollama at {rs.ollama_url}")
        except (OSError, ValueError) as e:
            check("research provider", "warn", f"Ollama unreachable at {rs.ollama_url}: {e}")
    n = Notifier()
    check(
        "alerts",
        "ok" if n.enabled else "warn",
        "Telegram configured" if n.enabled else "no LAB_TELEGRAM_TOKEN/LAB_TELEGRAM_CHAT_ID: alerts only in logs",
    )
    free_gb = shutil.disk_usage(cfg.root).free / 1e9
    check("disk", "ok" if free_gb > 2 else "warn", f"{free_gb:.1f} GB free")

    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    for name, status, detail in results:
        print(f"  {icon[status]} {name:<20} {detail}")
    fails = [r for r in results if r[1] == "fail"]
    print(
        f"\n{'READY' if not fails else 'NOT READY'}: {len(fails)} failure(s), "
        f"{sum(r[1] == 'warn' for r in results)} warning(s)"
    )
    return 1 if fails else 0


def cmd_notify_test(cfg, a):
    from lab.notify import Notifier

    n = Notifier()
    if not n.enabled:
        raise SystemExit("set LAB_TELEGRAM_TOKEN and LAB_TELEGRAM_CHAT_ID first (see README)")
    ok = n.send_now("test message from trading-lab")
    print("sent" if ok else "FAILED (check the token and chat id)")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lab", description="Research-only multi-agent trading lab.")
    p.add_argument("--config", help="path to lab.toml (default: search upwards from here)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("data", help="market data").add_subparsers(dest="sub", required=True)
    f = d.add_parser("fetch", help="download daily bars from Yahoo Finance (free)")
    f.add_argument("tickers", nargs="*")
    f.add_argument("--start")
    f.set_defaults(fn=cmd_data_fetch)
    i = d.add_parser("import", help="import a CSV (date, open, high, low, close, volume)")
    i.add_argument("file")
    i.add_argument("--ticker", required=True)
    i.add_argument("--date-column")
    i.add_argument("--adjusted", action="store_true", help="prices are split/dividend adjusted")
    i.add_argument(
        "--survivorship-free",
        action="store_true",
        help="the dataset includes delisted stocks (pair with `lab data membership`)",
    )
    i.add_argument("--source", help="name of the data vendor, recorded with every result")
    i.add_argument("--note")
    i.set_defaults(fn=cmd_data_import)
    d.add_parser("sample", help="store the offline real-data sample (GOOG 2004-2013)").set_defaults(fn=cmd_data_sample)
    mb = d.add_parser("membership", help="point-in-time universe: CSV of ticker,start,end membership spells")
    mb.add_argument("file")
    mb.set_defaults(fn=cmd_data_membership)
    d.add_parser("list", help="show stored data").set_defaults(fn=cmd_data_list)

    def period_args(x, default="train"):
        x.add_argument("--period", default=default, choices=["train", "test", "holdout"])
        x.add_argument("--start")
        x.add_argument("--end")
        x.add_argument("--tickers", nargs="+")
        x.add_argument("--set", action="append", help="override a parameter, e.g. sma_trend.fast=20")

    am = sub.add_parser("analyze", help="deterministic historical market/risk diagnostics (research only)")
    am.add_argument("--tickers", nargs="+")
    am.add_argument("--as-of", help="exchange-session date YYYY-MM-DD, strictly before the sealed holdout")
    am.add_argument("--lookback", type=int, default=252, help="history length in sessions")
    am.add_argument("--risk-window", type=int, default=63, help="rolling risk lookback in sessions")
    am.add_argument("--momentum-days", type=int, default=252, help="lookback for skip-recent momentum")
    am.add_argument("--skip-recent", type=int, default=21, help="exclude last N sessions from long momentum")
    am.add_argument("--shrinkage", type=float, default=0.15, help="sample covariance diagonal shrinkage")
    am.add_argument("--json", action="store_true", help="print full reproducible JSON report")
    am.add_argument("--output", help="write the JSON report to a new file (never overwrites)")
    am.set_defaults(fn=cmd_analyze)

    fm = sub.add_parser("forecast", help="read-only, purged probabilistic market research (never trades)")
    fm.add_argument("--ticker", required=True, help="one historical instrument")
    fm.add_argument("--as-of", help="exchange session YYYY-MM-DD strictly before sealed holdout")
    fm.add_argument("--horizon", type=int, default=1, help="forward close-to-close sessions (1-20)")
    fm.add_argument("--min-train", type=int, default=252, help="minimum training labels")
    fm.add_argument("--calibration", type=int, default=60, help="disjoint calibration labels")
    fm.add_argument("--test-sessions", type=int, default=60, help="final frozen OOS labels")
    fm.add_argument("--ridge-alpha", type=float, default=10.0, help="positive ridge regularization")
    fm.add_argument("--coverage", type=float, default=0.90, help="nominal split-conformal interval coverage")
    fm.add_argument("--drift-z-limit", type=float, default=5.0, help="abstain when feature z-distance exceeds")
    fm.add_argument("--json", action="store_true", help="full reproducible JSON with uncertainty caveats")
    fm.add_argument("--output", help="write a new JSON artifact; never overwrite existing evidence")
    fm.set_defaults(fn=cmd_forecast)

    ca = sub.add_parser("capital-audit", help="offline cash/FX/equity/bond stress audit (no trading)")
    ca.add_argument("--manifest", required=True, help="validated JSON portfolio, FX quotes and scenarios")
    ca.add_argument("--json", action="store_true", help="reproducible JSON evidence with factor attribution")
    ca.add_argument("--output", help="write a new JSON artifact without overwriting")
    ca.set_defaults(fn=cmd_capital_audit)

    b = sub.add_parser("backtest", help="compare signals against buy-and-hold")
    b.add_argument("signals", nargs="*", default=["sma_trend", "rsi_reversion"])
    b.add_argument(
        "--target-vol", type=float, help="volatility target for every portfolio, e.g. 0.15 (also the control)"
    )
    period_args(b)
    b.set_defaults(fn=cmd_backtest)

    n = sub.add_parser("null-test", help="test a signal against the bootstrap noise floor")
    n.add_argument("signal")
    n.add_argument("--samples", type=int, default=30)
    n.add_argument("--workers", type=int, default=None, help="parallel null-test workers (default: at most 4)")
    period_args(n)
    n.set_defaults(fn=cmd_null)

    wf = sub.add_parser("walk-forward", help="rolling in-sample optimization, out-of-sample evaluation")
    wf.add_argument("signal")
    wf.add_argument("--grid", nargs="+", required=True, help="parameter values, e.g. fast=20,50 slow=100,200")
    wf.add_argument("--is-years", type=int, default=4)
    wf.add_argument("--oos-years", type=int, default=1)
    wf.add_argument("--start")
    wf.add_argument("--end")
    wf.add_argument("--tickers", nargs="+")
    wf.set_defaults(fn=cmd_walk_forward)

    pb = sub.add_parser("pbo", help="probability of backtest overfitting across a parameter grid")
    pb.add_argument("signal")
    pb.add_argument("--grid", nargs="+", required=True)
    pb.add_argument("--splits", type=int, default=10)
    period_args(pb)
    pb.set_defaults(fn=cmd_pbo)

    h = sub.add_parser("holdout", help="the one-time final exam on the holdout period (logged)")
    h.add_argument("signal")
    h.add_argument("--tickers", nargs="+")
    h.add_argument("--set", action="append")
    h.add_argument("--again", metavar="REASON", help="repeat a holdout run; the reason is recorded")
    h.set_defaults(fn=cmd_holdout)

    r = sub.add_parser("research", help="the AI research swarm").add_subparsers(dest="sub", required=True)
    rr = r.add_parser("run", help="run research cycles: propose, implement, review, validate")
    rr.add_argument("--cycles", type=int, default=1)
    rr.add_argument("--provider", choices=["claude_code", "ollama"])
    rr.add_argument("--model")
    rr.set_defaults(fn=cmd_research_run)
    r.add_parser("list", help="all hypotheses and their status").set_defaults(fn=cmd_research_list)
    rt = r.add_parser("retry", help="re-implement a hypothesis that never reached validation")
    rt.add_argument("id")
    rt.add_argument("--provider", choices=["claude_code", "ollama"])
    rt.add_argument("--model")
    rt.set_defaults(fn=cmd_research_retry)
    rs = r.add_parser("show", help="details of one hypothesis")
    rs.add_argument("id")
    rs.set_defaults(fn=cmd_research_show)
    rp = r.add_parser("promote", help="approve a candidate for paper trading (human decision)")
    rp.add_argument("id")
    rp.add_argument("--yes", action="store_true")
    rp.set_defaults(fn=cmd_research_promote)

    c = sub.add_parser("llm-check", help="verify the AI provider works (one tiny call)")
    c.add_argument("--provider", choices=["claude_code", "ollama"])
    c.add_argument("--model")
    c.set_defaults(fn=cmd_llm_check)

    pp = sub.add_parser("paper", help="paper trade on Interactive Brokers (paper accounts only)")
    pp.add_argument("--signals", nargs="+", default=["sma_trend"])
    pp.add_argument("--tickers", nargs="+")
    pp.add_argument("--set", action="append")
    pp.add_argument("--check-only", action="store_true", help="validate the setup without connecting")
    pp.add_argument("--smoke", action="store_true", help="connect, verify account/instruments/data, no trading")
    pp.add_argument(
        "--test-order", action="store_true", help="with --smoke: place and cancel an unfillable 1-share limit order"
    )
    pp.add_argument(
        "--adopt-broker", action="store_true", help="reset the agents' ledger to the broker's current positions"
    )
    pp.set_defaults(fn=cmd_paper)

    sub.add_parser(
        "paper-report", help="paper results, costs and implementation shortfall from the journal"
    ).set_defaults(fn=cmd_paper_report)
    k = sub.add_parser("kill", help="kill switch: stop all new orders now")
    k.add_argument("--flatten", action="store_true", help="also exit every position at the next open")
    k.add_argument("--reason", default="manual")
    k.set_defaults(fn=cmd_kill)
    sub.add_parser("resume", help="clear the kill switch").set_defaults(fn=cmd_resume)
    hl = sub.add_parser("health", help="exit 0 if the agents' heartbeat is fresh (for Docker)")
    hl.add_argument("--max-age", type=int, default=180)
    hl.set_defaults(fn=cmd_health)
    sub.add_parser("doctor", help="pre-flight check of the whole deployment").set_defaults(fn=cmd_doctor)
    sub.add_parser("notify-test", help="send a test alert").set_defaults(fn=cmd_notify_test)

    sub.add_parser("signals", help="list available signals").set_defaults(fn=None)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    pd.set_option("display.width", 160)
    if a.cmd == "signals":
        for name, cls in BUILTIN.items():
            print(f"{name:<16} built-in   {cls.description}")
        for name in available_research(cfg.research_dir):
            print(f"{name:<16} research")
        return 0
    try:
        return a.fn(cfg, a) or 0
    except (data.DataError, ConfigError, MarketAnalysisError, MultiAssetError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
