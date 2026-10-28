"""lab: command-line interface for the trading lab."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from lab import data, experiments, fast_research
from lab.borrow import BorrowUnavailable
from lab.config import ConfigError, LabConfig, Period, load_config
from lab.signals.builtin import BUILTIN
from lab.signals.registry import SignalNotFound, available_research, resolve


def _period(cfg: LabConfig, a) -> Period:
    if a.start or a.end:
        start = date.fromisoformat(a.start) if a.start else cfg.period(a.period).start
        end = date.fromisoformat(a.end) if a.end else None
        period = Period("custom", start, end)
    else:
        period = cfg.period(a.period)
    if experiments.overlaps_holdout(cfg, period.start, period.end):
        # Judged by the dates, not the name (G007-F7): explicit dates cannot route around the guard.
        raise SystemExit("the holdout is guarded: these dates overlap it; use `lab holdout <signal>`")
    return period


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
    if a.speed != "daily":
        return _fetch_intraday(cfg, tickers, a.speed)
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


def _fetch_intraday(cfg, tickers, name):
    from lab import intraday

    sp = intraday.speed(name)
    failed = []
    for t in tickers:
        try:
            e = intraday.save(cfg, t, intraday.fetch_yfinance(t, sp), sp, source="yahoo_finance", adjusted=True)
            dropped = f", {len(e['dropped_incomplete'])} incomplete bar(s) dropped" if e["dropped_incomplete"] else ""
            print(
                f"{t:<6} {e['rows']:>6} {sp.name} bars, {e['sessions']} sessions  {e['first']} .. {e['last']}{dropped}"
            )
        except data.DataError as err:
            failed.append(t)
            print(f"{t:<6} FAILED: {err}")
    print(f"note: Yahoo serves about {sp.yahoo_period} of {sp.name} bars; intraday research rests on far less history")
    return 1 if failed else 0


def cmd_data_import(cfg, a):
    if a.speed != "daily":
        from lab import intraday

        sp = intraday.speed(a.speed)
        df = data.import_csv(a.file, a.date_column, keep_time=True)
        e = intraday.save(cfg, a.ticker, df, sp, source=a.source or f"csv:{a.file}", adjusted=a.adjusted)
        print(f"{a.ticker.upper()}: {e['rows']} {sp.name} bars, {e['sessions']} sessions {e['first']} .. {e['last']}")
        return None
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


def cmd_data_risk_free(cfg, a):
    if a.file:
        df = data.save_risk_free(cfg, pd.read_csv(a.file), source=f"csv:{a.file}")
    else:
        df = data.save_risk_free(cfg, data.fetch_risk_free(), source="FRED DTB3 (3-month Treasury bill)")
    print(
        f"risk-free rate: {len(df)} days {df['date'].iloc[0].date()} .. {df['date'].iloc[-1].date()} "
        f"-> {data.risk_free_path(cfg).relative_to(cfg.root)}; Sharpe and Sortino now use excess returns"
    )


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
    from lab import intraday

    m = data.manifest(cfg)
    quick = {n: intraday.manifest(cfg, sp) for n, sp in intraday.SPEEDS.items() if not sp.daily}
    if not m and not any(quick.values()):
        print("no data yet: lab data fetch")
        return
    for t, e in sorted(m.items()):
        print(
            f"{t:<6} {e['rows']:>5} days  {e['first']} .. {e['last']}  {e['source']}{'' if e['adjusted'] else ' (unadjusted)'}"
        )
    for name, man in quick.items():
        for t, e in sorted(man.items()):
            print(
                f"{t:<6} {e['rows']:>6} {name} bars, {e['sessions']} sessions  {e['first']} .. {e['last']}  {e['source']}"
            )


# ----------------------------------------------------------------------------- experiments


def _specs(cfg, names, sets, require_tradable=False):
    params = _params(sets)
    try:
        return [resolve(n, cfg.research_dir, params.get(n), require_tradable=require_tradable) for n in names]
    except (SignalNotFound, ValueError) as e:
        raise SystemExit(str(e)) from e


def _with_target_vol(cfg, a):
    import dataclasses
    import math

    from lab.risk_profiles import PROFILES

    tv = getattr(a, "target_vol", None)
    if tv is None:
        return cfg
    if not math.isfinite(tv) or tv < 0:
        raise SystemExit("--target-vol must be a finite number >= 0")
    # A selected profile's volatility budget is a ceiling, on every route (G007-F9, found by ChatGPT in its
    # own profile work): an override may tighten it, never raise it.
    cap = PROFILES[cfg.risk.profile].target_vol if cfg.risk.profile in PROFILES else None
    if cap is not None and (tv == 0 or tv > cap):
        raise SystemExit(
            f"--target-vol {tv:g} exceeds the {cfg.risk.profile} profile's volatility budget {cap:g} "
            "(0 would switch it off); choose another profile or a value <= the budget"
        )
    return dataclasses.replace(cfg, portfolio=dataclasses.replace(cfg.portfolio, target_vol=tv))


INTRADAY_DEFAULTS = ["orb", "intraday_momentum", "vwap_reversion", "value_area"]


def _speed(cfg, a) -> str:
    from lab import intraday

    name = getattr(a, "speed", None) or cfg.trading.speed
    try:
        intraday.check_speed_allowed(name, cfg.risk.profile)
    except ValueError as e:
        raise SystemExit(f"{e}; set [risk] profile in lab.toml") from e
    return name


def _intraday_period(cfg, a, tickers, name):
    """The intraday research period (everything before the intraday holdout), or explicit dates inside it."""
    from lab import intraday

    frames = intraday.load_many(cfg, tickers, intraday.speed(name))
    research, _ = experiments.intraday_split(cfg, frames)
    if a.start or a.end:
        start = date.fromisoformat(a.start) if a.start else research.start
        end = date.fromisoformat(a.end) if a.end else research.end
        return Period("custom", start, end), frames
    return research, frames


def cmd_backtest(cfg, a):
    if a.period == "holdout" and not (a.start or a.end):
        raise SystemExit("the holdout is guarded: use `lab holdout <signal>`")
    cfg = _with_target_vol(cfg, a)
    speed = _speed(cfg, a)
    specs = _specs(cfg, a.signals or (INTRADAY_DEFAULTS if speed != "daily" else ["sma_trend", "rsi_reversion"]), a.set)
    tickers = _tickers(cfg, a.tickers)
    if speed != "daily":
        period, frames = _intraday_period(cfg, a, tickers, speed)
        try:
            comp, results = experiments.compare(cfg, specs, tickers, period, frames, speed=speed)
        except experiments.HoldoutUsed as e:
            raise SystemExit(str(e)) from e
        _print_table(comp)
        print(f"\n{comp.data_quality['intraday_history']}")
        print(
            f"speed {speed}: decisions on each bar as it arrives ({cfg.trading.data_delay_min:g} min data delay, "
            f"rounded up to whole bars), fills at the next opening print; "
            f"{'flat at every close' if cfg.trading.flat_at_close else 'positions held overnight'}"
        )
        out = experiments.save_run(cfg, f"backtest-{speed}", comp, results)
        print(f"saved: {out.relative_to(cfg.root)}  (report: {(out / 'report.html').relative_to(cfg.root)})")
        return
    comp, results = experiments.compare(cfg, specs, tickers, _period(cfg, a))
    _print_table(comp)
    for r in results:
        if r.signal_errors or r.rejections:
            print(f"  note: {r.label}: {r.signal_errors} signal errors, {r.rejections} rejected orders")
    out = experiments.save_run(cfg, "backtest", comp, results)
    print(f"\nsaved: {out.relative_to(cfg.root)}  (report: {(out / 'report.html').relative_to(cfg.root)})")


def cmd_null(cfg, a):
    if a.samples <= 0:
        raise ConfigError("--samples must be positive")
    (spec,) = _specs(cfg, [a.signal], a.set)
    tickers = _tickers(cfg, a.tickers)
    period = _period(cfg, a)
    if period.name == "holdout":
        raise SystemExit("the holdout is guarded: use `lab holdout <signal>`")
    frames = data.load_many(cfg, tickers)
    comp, results = experiments.compare(cfg, [spec], tickers, period, frames)
    _print_table(comp)
    print(f"\nnoise floor: {a.samples} block-bootstrap resamples of the real data...")
    nt = experiments.null_test(cfg, spec, tickers, period, a.samples, frames, observed_edge=comp.rows[1]["sharpe_edge"])
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
    if a.is_years <= 0 or a.oos_years <= 0:
        raise ConfigError("walk-forward window lengths must be positive")
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
    speed = _speed(cfg, a)
    try:
        experiments.check_holdout_allowed(cfg, spec, a.again or "", speed=speed)
    except experiments.HoldoutUsed as e:
        raise SystemExit(str(e)) from e
    if speed == "daily":
        comp, results = experiments.compare(
            cfg, [spec], tickers, cfg.period("holdout"), holdout_ok=True, holdout_reason=a.again or ""
        )
    else:
        from lab import intraday

        frames = intraday.load_many(cfg, tickers, intraday.speed(speed))
        _, period = experiments.intraday_split(cfg, frames)
        comp, results = experiments.compare(
            cfg, [spec], tickers, period, frames, holdout_ok=True, speed=speed, holdout_reason=a.again or ""
        )
    experiments.record_holdout(cfg, spec, comp, a.again or "", speed=speed)
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
    from lab.research.approval import ApprovalError, write_approval
    from lab.research.orchestrator import Hypothesis

    h = Hypothesis(cfg.research_dir, a.id)
    if h.status != "candidate":
        raise SystemExit(f"{a.id} is {h.status!r}; only candidates (passed train AND test) can be promoted")
    source = (h.dir / "signal.py").read_bytes()  # what is shown is exactly what is approved and hashed
    print(source.decode("utf-8"))
    if not a.yes and input("Read the code above. Promote it to paper trading? [y/N] ").strip().lower() != "y":
        raise SystemExit("not promoted")
    try:
        rec = write_approval(h.dir, source, {}, replace(cfg, trading=replace(cfg.trading, speed="daily")).snapshot())
    except ApprovalError as e:
        raise SystemExit(str(e)) from e
    h.set_status("promoted", f"human approval; source sha256 {rec['source_sha256'][:16]}")
    print(f"{a.id} promoted; it can now be used with: lab paper --signals {a.id}")


# ----------------------------------------------------------------------------- active profile


def cmd_risk_profile(cfg, a):
    """Read-only audit of the effective profile already wired into paper/backtest."""
    from dataclasses import asdict

    from lab.risk_profiles import profile_bindings

    report = {
        "profile": cfg.risk.profile,
        "bound_by_operator_limits": profile_bindings(cfg),
        "status": "ACTIVE_CONFIGURATION_PAPER_ONLY",
        "risk": asdict(cfg.risk),
        "portfolio": asdict(cfg.portfolio),
        "broker_shortability_verified": False,
        "real_money_authorized": False,
    }
    if a.json:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(f"profile {report['profile']} (paper accounts only)")
        for k, v in report["risk"].items():
            if k != "profile":
                print(f"  risk.{k}: {v}")
        for k, v in report["portfolio"].items():
            print(f"  portfolio.{k}: {v}")
        for note in report["bound_by_operator_limits"]:
            print(f"  NOTE {note}")
        print("borrow locate and broker permissions require independent verification")
    return 0


# ----------------------------------------------------------------------------- paper


_HELD_LOCKS: list = []  # runtime locks: closing (or garbage-collecting) one would release it


def cmd_paper(cfg, a):
    import asyncio

    from lab.notify import Notifier
    from lab.paper import RuntimeLocked, acquire_runtime_lock, build_node, build_smoke_node, runtime_dir
    from lab.research.approval import ApprovalError
    from lab.safety import SafetyError

    tickers = [t.upper() for t in (a.tickers or cfg.universe)]
    try:
        if a.gold and a.smoke:
            raise ValueError("--gold cannot be combined with --smoke")
        _HELD_LOCKS.append(acquire_runtime_lock(cfg))
        notifier = Notifier(spool_path=str(runtime_dir(cfg) / "notifications.sqlite3"))
        if a.gold and not a.check_only:
            from lab.gold_data import download_gold_history

            count = asyncio.run(download_gold_history(cfg.paper, runtime_dir(cfg) / "gold-history.json"))
            print(f"IBKR London Gold: downloaded {count} closed daily bars")
        if a.smoke:
            node, report = build_smoke_node(cfg, tickers, test_order=a.test_order)
        else:
            # Research signals trade only as the approved artifact: the verified spec carries the approved
            # source digest, and the node starts from it (G007-F5).
            specs = [
                resolve(s.name, cfg.research_dir, s.params, require_tradable=True) if s.path is not None else s
                for s in _specs(cfg, a.signals, a.set)
            ]
            node = build_node(
                cfg,
                specs,
                tickers,
                gold=a.gold,
                adopt_broker=a.adopt_broker,
                notifier=notifier,
                operator_control=not a.check_only,
                bank_path=a.bank,
                mode=a.mode,
                authority_hours=a.authority_hours,
            )
    except (SafetyError, ValueError, SignalNotFound, RuntimeLocked, ApprovalError) as e:
        raise SystemExit(str(e)) from e
    what = (
        "smoke test"
        if a.smoke
        else f"{len(a.signals)} signal(s) x {len(tickers)} stocks" + (" + London Gold ATR" if a.gold else "")
    )
    print(f"paper node ready: {what}, account {cfg.paper.account_id} via {cfg.paper.host}:{cfg.paper.port}")
    if not a.smoke:
        t = cfg.trading
        pace = (
            "daily: decide after the close, market-on-open orders"
            if t.speed == "daily"
            else f"{t.speed} bars, {t.data_delay_min:g} min data delay, limit collar {t.limit_collar:.1%}, "
            + ("flat at every close" if t.flat_at_close else "positions held overnight")
        )
        print(f"profile {cfg.risk.profile}, speed {pace}")
    if a.check_only:
        node.dispose()
        print("check only: configuration is valid; not connecting.")
        return 0
    if not a.smoke:
        notifier.send(f"paper trading starting: {what}")
    print("connecting to Interactive Brokers... (Ctrl+C to stop)")
    try:
        node.run()
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        node.dispose()
        if not notifier.flush():
            print("ALERT DELIVERY PENDING: durable outbox will retry on restart", file=sys.stderr)
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


def cmd_record(cfg, a):
    from lab.record import build_node, saved

    out = Path(a.out) if a.out else cfg.data_dir / "market"
    if a.status:
        kinds = saved(out)
        if not kinds:
            raise SystemExit(f"nothing saved in {out / 'catalog'} yet (data is saved every hour and at a clean stop)")
        for kind, instruments in kinds.items():
            print(f"{kind:20} " + "  ".join(f"{i}: {n} file(s)" for i, n in instruments.items()))
        return 0
    try:
        node, _ = build_node(a.symbols, out)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    symbols = " ".join(s.upper() for s in a.symbols)
    print(f"market-data recorder ready: Binance USD-M {symbols} (order book, trades, mark price, funding) -> {out}")
    if a.check_only:
        node.dispose()
        print("check only: configuration is valid; not connecting.")
        return 0
    print("connecting to Binance public market data... (Ctrl+C to stop)")
    try:
        node.run()
    except KeyboardInterrupt:
        print("stopping...")
    except RuntimeError as e:
        # Exits instead of retrying in-process: NautilusTrader keeps one LiveNode per process, and a Ctrl+C during
        # a failed connection is otherwise lost. The compose `recorder` service restarts it.
        raise SystemExit(f"recorder stopped: {e}") from e
    finally:
        node.dispose()
    return 0


def cmd_micro_study(cfg, a):
    from lab.micro_study import StudySettings, study
    from lab.record import perpetual_ids

    catalog = Path(a.catalog) if a.catalog else cfg.data_dir / "market" / "catalog"
    if not catalog.exists():
        raise SystemExit(f"no recorded market data at {catalog}; run `lab record` first")
    try:
        iid = str(perpetual_ids([a.symbol])[0])
    except ValueError as e:
        raise SystemExit(str(e)) from e
    start, end = (int(pd.Timestamp(x, tz="UTC").value) if x else None for x in (a.start, a.end))
    try:
        start, end = fast_research.research_window(cfg.results_dir, str(catalog), iid, start, end)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    settings = StudySettings(
        every_ms=a.every_ms,
        horizons_s=tuple(a.horizons),
        taker_fee_bps=a.fee_bps,
        flow_bucket_notional=a.bucket,
    )
    samples, table = study(str(catalog), iid, settings, start, end)
    if samples.empty:
        raise SystemExit(f"no order-book data for {iid} in {catalog} before its holdout")
    first, last = (pd.Timestamp(samples["ts"].iloc[i], tz="UTC") for i in (0, -1))
    params = {"every_ms": a.every_ms, "horizons_s": list(a.horizons), "fee_bps": a.fee_bps, "bucket": a.bucket}
    n = fast_research.register_trial(
        cfg.results_dir, "micro-study", iid, params, (start, end),
        {"ic": {f"{r.feature}@{r.horizon_s:g}s": r.ic for r in table.itertuples() if r.ic == r.ic}},
    )  # fmt: skip
    print(f"{iid}: {len(samples):,} samples every {a.every_ms} ms, {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC")
    print(
        f"research data only (holdout from {pd.Timestamp(end + 1, tz='UTC'):%Y-%m-%d %H:%M} UTC); study settings tried: {n}"
    )
    print(table.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(
        "ic: rank correlation with the forward mid return. top/bottom: mean forward return (bps) in the feature's"
        " top/bottom decile; it pays only where it exceeds cost_bps (taker round trip + median spread)."
    )
    return 0


def _fast_config(cfg, a, *, kill_file=None):
    from lab.fast import ImbalanceTakerConfig
    from lab.record import perpetual_ids

    try:
        return ImbalanceTakerConfig(
            instrument_id=str(perpetual_ids([a.symbol])[0]),
            trade_size=a.size,
            threshold=a.threshold,
            hold_s=a.hold,
            max_loss=a.max_loss,
            kill_file=kill_file,
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e


def cmd_fast_backtest(cfg, a):
    from lab.fast import backtest

    catalog = Path(a.catalog) if a.catalog else cfg.data_dir / "market" / "catalog"
    if not catalog.exists():
        raise SystemExit(f"no recorded market data at {catalog}; run `lab record` first")
    start, end = (int(pd.Timestamp(x, tz="UTC").value) if x else None for x in (a.start, a.end))
    config = _fast_config(cfg, a)
    iid = config.instrument_id
    params = {
        "strategy": "imbalance_taker", "size": a.size, "threshold": a.threshold, "hold_s": a.hold,
        "max_loss": a.max_loss, "capital": a.capital, "taker_bps": a.fee_bps, "maker_bps": a.maker_bps,
        "latency_ms": a.latency_ms,
    }  # fmt: skip
    try:
        if a.holdout:
            if a.start or a.end:
                raise SystemExit("--holdout runs on the whole holdout; --start/--end don't apply")
            start = fast_research.boundary(cfg.results_dir, str(catalog), iid)
            fast_research.check_holdout(cfg.results_dir, iid, params, start, a.again or "")
        else:
            if a.again:
                raise SystemExit("--again only applies with --holdout")
            start, end = fast_research.research_window(cfg.results_dir, str(catalog), iid, start, end)
        r = backtest(
            str(catalog),
            config,
            start=start,
            end=end,
            capital=a.capital,
            maker_bps=a.maker_bps,
            taker_bps=a.fee_bps,
            latency_ms=a.latency_ms,
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e
    result = {k: r[k] for k in ("entries", "pnl", "commissions", "round_trips", "mean_trip", "t_stat", "stopped")}
    if a.holdout:
        fast_research.record_holdout(cfg.results_dir, iid, params, start, result, a.again or "")
    n = fast_research.register_trial(
        cfg.results_dir, "fast-backtest", iid, params, (r["start_ns"], r["end_ns"]), result, holdout=a.holdout
    )
    first, last = (pd.Timestamp(r[k], tz="UTC") for k in ("start_ns", "end_ns"))
    print(f"{iid} imbalance taker, {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC")
    print(
        f"  {'HOLDOUT test (recorded)' if a.holdout else 'research data'}; configurations tried on research data: {n}"
    )
    print(
        f"  entries {r['entries']}, fills {r['fills']}, commissions {r['commissions']:.4f}, "
        f"P&L after costs {r['pnl']:+.4f} (quote currency)"
    )
    print(f"  round trips {r['round_trips']}, mean {r['mean_trip']:+.5f} each, t-stat {r['t_stat']:+.2f}")
    if r["stopped"]:
        print(f"  stopped: {r['stopped']}")
    return 0


def cmd_fast_release(cfg, a):
    from lab.record import perpetual_ids

    catalog = Path(a.catalog) if a.catalog else cfg.data_dir / "market" / "catalog"
    try:
        iid = str(perpetual_ids([a.symbol])[0])
        old, new = fast_research.release(cfg.results_dir, str(catalog), iid, a.reason)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    print(
        f"{iid}: data {pd.Timestamp(old, tz='UTC'):%Y-%m-%d %H:%M} .. {pd.Timestamp(new, tz='UTC'):%Y-%m-%d %H:%M} UTC "
        "is now research data; the new holdout is everything recorded from here on"
    )
    return 0


def cmd_fast_paper(cfg, a):
    from lab.fast import build_paper_node
    from lab.paper import kill_path

    node, _ = build_paper_node(
        _fast_config(cfg, a, kill_file=str(kill_path(cfg))), capital=a.capital, taker_bps=a.fee_bps,
        maker_bps=a.maker_bps,
    )  # fmt: skip
    print(
        f"fast paper node ready: {a.symbol.upper()} imbalance taker on live Binance data, simulated fills "
        f"(no exchange orders, no keys); `lab kill` stops it"
    )
    if a.check_only:
        node.dispose()
        print("check only: configuration is valid; not connecting.")
        return 0
    try:
        node.run()
    except KeyboardInterrupt:
        print("stopping...")
    except RuntimeError as e:
        raise SystemExit(f"fast paper node stopped: {e}") from e
    finally:
        node.dispose()
    return 0


def cmd_runtime(cfg, a):
    from lab.runtime_ops import run

    try:
        result = run(cfg, a.action, destination=a.destination, evidence=a.evidence, reason=a.reason)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2))
    return 0


def cmd_control(cfg, a):
    from lab.authority import Authority
    from lab.paper import runtime_dir
    from lab.persistence import read_json

    rt = runtime_dir(cfg)
    control = Authority(rt, read_json(rt / "identity.json"))
    if a.action == "status":
        print(json.dumps({"mandate": control.read(allow_expired=True), "requests": control.requests()}, indent=2))
    elif a.action == "approve":
        if not a.request:
            raise SystemExit("control approve needs --request from control status")
        control.approve(a.request)
        print("Approved once, for the exact proposed order and book; expires within one hour.")
    else:
        previous = control.read(allow_expired=True)
        targets = dict(previous["targets"])
        if a.target:
            targets = {t.upper(): float(w) for t, w in (item.split("=", 1) for item in a.target)}
        mode = a.mode or previous["mode"]
        if a.target and mode != "manual":
            raise SystemExit("--target requires manual mode")
        print(json.dumps(control.issue(mode, a.hours, targets), indent=2))
    return 0


def cmd_paper_report(cfg, a):
    from lab.paper import runtime_dir
    from lab.paper_report import load_journal, summarize

    journals = sorted(runtime_dir(cfg).glob("journal-*.jsonl"))
    if not journals:
        raise SystemExit("no paper-trading journal yet (results/paper/journal-*.jsonl)")
    r = summarize(load_journal(journals))
    if "last_equity" in r:
        return_text = (
            f"{r['total_return']:+.2%}" if r["total_return"] is not None else "unqualified return (external flows)"
        )
        print(
            f"{r['start']} .. {r['end']}: equity {r['first_equity']:,.2f} -> {r['last_equity']:,.2f} "
            f"({return_text}), max drawdown {r['max_drawdown']:.1%}"
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


def cmd_recover(cfg, a):
    """Clear a hard halt (lab/halt.py) after an operator has looked at it. Audited."""
    from lab.halt import HARD_KINDS, clear_halt, halt_kinds, read_halt
    from lab.notify import Notifier
    from lab.paper import adopt_path, dispose_path, halt_path, recoveries_path

    halt = read_halt(halt_path(cfg))
    if halt is None:
        print("no hard halt is active")
        return 0
    print(f"hard halt: {halt['kind']} ({HARD_KINDS.get(halt['kind'], '')})")
    print(f"  since {halt.get('at')}: {halt.get('reason', '')}")
    for h in halt.get("history", []):
        print(f"  then {h['at']}: {h['kind']}: {h['reason']}")
    if not a.reason:
        print(
            '\nto clear it: lab recover --reason "what you checked and why it is safe"'
            + (
                "  [--adopt-broker | --dispose-order ID --evidence ...]"
                if "reconciliation_mismatch" in halt_kinds(halt)
                else ""
            )
        )
        return 1
    if a.dispose_order and not (a.evidence or "").strip():
        raise SystemExit("--dispose-order needs --evidence: what IBKR's order and trade history shows for it")
    if "reconciliation_mismatch" in halt_kinds(halt) and not (a.adopt_broker or a.dispose_order):
        raise SystemExit(
            "a reconciliation mismatch is cleared by deciding which book is right: add --adopt-broker to take the "
            "broker's positions as the truth (after checking the broker account), or, for an order the broker "
            "never reported, --dispose-order ID --evidence '...' after checking its order and trade history; "
            "or fix the cause and re-run"
        )
    if halt["kind"] == "paper_guard":
        print("note: the account guard will stop trading again unless the broker now reports the paper account")
    if a.dispose_order:
        p = dispose_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "orders": a.dispose_order,
                    "evidence": a.evidence,
                    "reason": a.reason,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
        )
    if a.adopt_broker:
        p = adopt_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"reason": a.reason, "at": datetime.now(UTC).isoformat(timespec="seconds")}))
    clear_halt(halt_path(cfg), a.reason, audit=recoveries_path(cfg))
    msg = (
        f"hard halt {halt['kind']} cleared by operator: {a.reason}"
        + ("; the ledger adopts the broker's positions at the next rebalance" if a.adopt_broker else "")
        + (
            f"; at the next start the agents dispose of {', '.join(a.dispose_order)} on evidence: {a.evidence}"
            if a.dispose_order
            else ""
        )
    )
    print(msg + f". Recorded in {recoveries_path(cfg).relative_to(cfg.root)}")
    Notifier().send_now(msg)
    return 0


def _heartbeat_age(cfg):
    from lab.paper import heartbeat_path

    p = heartbeat_path(cfg)
    if not p.exists():
        return None, None
    hb = json.loads(p.read_text())
    if not isinstance(hb, dict):
        raise ValueError("heartbeat is not a JSON object")
    at = datetime.fromisoformat(hb["at"])
    if at.tzinfo is None:
        raise ValueError("heartbeat time has no time zone")
    age = (datetime.now(UTC) - at).total_seconds()
    return age, hb


def cmd_health(cfg, a):
    """Exit 0: alive (and, with --ready, able to trade). 1: not alive. 2: alive but not ready (--ready).

    Alive: a well-formed heartbeat, recent, not from the future, from a node that has not stopped (G007-F12:
    a fresh heartbeat written at a clean stop used to count as healthy). A halted node is alive by design:
    restarting it would not help, so Docker's health check uses liveness only."""
    try:
        age, hb = _heartbeat_age(cfg)
    except (ValueError, KeyError, TypeError) as e:
        print(f"unhealthy: unreadable heartbeat ({e})")
        return 1
    if age is None:
        print("unhealthy: no heartbeat yet")
        return 1
    problems = []
    if age < -60:
        problems.append(f"heartbeat is {-age:.0f}s in the future")
    elif age > a.max_age:
        problems.append(f"last heartbeat {age:.0f}s ago")
    if hb.get("running") is not True:
        problems.append("the node reported a stop")
    if problems:
        print("unhealthy: " + "; ".join(problems) + f"; {json.dumps(hb)}")
        return 1
    if getattr(a, "ready", False):
        blockers = [f"{k}={hb[k]}" for k in ("hard_halt", "kill_switch", "blocked") if hb.get(k)]
        if blockers:
            print("alive, not ready: " + ", ".join(blockers))
            return 2
    print(f"healthy: last heartbeat {age:.0f}s ago; {json.dumps(hb)}")
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
    try:
        age, hb = _heartbeat_age(cfg)
    except (ValueError, KeyError, TypeError) as e:
        age, hb = None, {"error": str(e)}
    running = bool(hb and hb.get("running"))
    check(
        "agents heartbeat",
        "ok" if age is not None and 0 <= age < 180 and running else "warn",
        "no heartbeat (not running)"
        if age is None
        else f"last {age:.0f}s ago{'' if running else ', node reported a stop'}",
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

    app = sub.add_parser("app", help="open guided setup and local paper-trading controls")
    app.add_argument("--directory", default="~/TradingLab", help="private app workspace")
    app.add_argument("--port", type=int, default=8765)
    app.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    app.add_argument("--no-browser", action="store_true")

    d = sub.add_parser("data", help="market data").add_subparsers(dest="sub", required=True)
    f = d.add_parser("fetch", help="download daily bars from Yahoo Finance (free)")
    f.add_argument("tickers", nargs="*")
    f.add_argument("--start")
    f.add_argument(
        "--speed",
        default="daily",
        choices=["daily", "1h", "15m", "5m"],
        help="bar size; intraday history is short (Yahoo: ~2 years of 1h, ~60 days of 15m/5m)",
    )
    f.set_defaults(fn=cmd_data_fetch)
    i = d.add_parser("import", help="import a CSV (date, open, high, low, close, volume)")
    i.add_argument("file")
    i.add_argument("--ticker", required=True)
    i.add_argument("--date-column")
    i.add_argument(
        "--speed",
        default="daily",
        choices=["daily", "1h", "15m", "5m"],
        help="intraday CSVs: timestamps are bar STARTS in New York time, regular hours only",
    )
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
    rf = d.add_parser("risk-free", help="daily risk-free rate for excess-return Sharpe/Sortino (FRED DTB3, free)")
    rf.add_argument("--file", help="CSV with date and rate (annual percent) instead of downloading from FRED")
    rf.set_defaults(fn=cmd_data_risk_free)
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

    b = sub.add_parser("backtest", help="compare signals against buy-and-hold")
    b.add_argument(
        "signals", nargs="*", help="default: sma_trend rsi_reversion (daily); orb intraday_momentum vwap_reversion"
    )
    b.add_argument(
        "--speed",
        choices=["daily", "1h", "15m", "5m"],
        help="default: [trading] speed; quick speeds need the dynamic or aggressive profile and intraday data",
    )
    b.add_argument(
        "--target-vol", type=float, help="volatility target for every portfolio, e.g. 0.15 (also the control)"
    )
    period_args(b)
    b.set_defaults(fn=cmd_backtest)

    n = sub.add_parser("null-test", help="test a signal against the bootstrap noise floor")
    n.add_argument("signal")
    n.add_argument("--samples", type=int, default=30)
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
    h.add_argument("--speed", choices=["daily", "1h", "15m", "5m"], help="default: [trading] speed")
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

    rp = sub.add_parser("risk-profile", help="show active risk budget and exposure ceilings")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=cmd_risk_profile)

    pp = sub.add_parser("paper", help="paper trade on Interactive Brokers (paper accounts only)")
    pp.add_argument("--signals", nargs="+", default=["sma_trend"])
    pp.add_argument("--tickers", nargs="+")
    pp.add_argument("--gold", action="store_true", help="include London Gold ATR PAPER strategy")
    pp.add_argument("--set", action="append")
    pp.add_argument("--bank", help="predeclared daily strategy-bank manifest")
    pp.add_argument(
        "--mode",
        choices=["autonomous", "supervised", "manual"],
        help="explicitly issue a new operating mandate; default preserves existing mode",
    )
    pp.add_argument(
        "--authority-hours",
        type=float,
        default=None,
        help="opt-in time limit for a new mandate, at most 168 hours (default: an autonomous mandate is standing, "
        "supervised and manual last 24 hours); restarts never renew a lease",
    )
    pp.add_argument("--check-only", action="store_true", help="validate the setup without connecting")
    pp.add_argument("--smoke", action="store_true", help="connect, verify account/instruments/data, no trading")
    pp.add_argument(
        "--test-order", action="store_true", help="with --smoke: place and cancel an unfillable 1-share limit order"
    )
    pp.add_argument(
        "--adopt-broker", action="store_true", help="reset the agents' ledger to the broker's current positions"
    )
    pp.set_defaults(fn=cmd_paper)

    from lab.record import DEFAULT_SYMBOLS

    rec = sub.add_parser("record", help="record crypto order books, trades, mark prices and funding (data only)")
    rec.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS), help="Binance USD-M perpetuals")
    rec.add_argument("--out", help="output directory (default: <data>/market)")
    rec.add_argument("--check-only", action="store_true", help="validate the setup without connecting")
    rec.add_argument("--status", action="store_true", help="list what has been saved to the catalog, then exit")
    rec.set_defaults(fn=cmd_record)

    ms = sub.add_parser("micro-study", help="do order-book and trade-flow features predict moves beyond costs?")
    ms.add_argument("symbol", help="a recorded Binance USD-M perpetual, e.g. BTCUSDT")
    ms.add_argument("--catalog", help="catalog directory (default: <data>/market/catalog)")
    ms.add_argument("--start", help="UTC start, e.g. 2026-11-01")
    ms.add_argument("--end", help="UTC end")
    ms.add_argument("--every-ms", type=int, default=100, help="sampling clock (default 100 ms)")
    ms.add_argument("--horizons", type=float, nargs="+", default=[1.0, 5.0, 30.0], help="seconds ahead")
    ms.add_argument("--fee-bps", type=float, default=5.0, help="taker fee per side in bps (default 5)")
    ms.add_argument("--bucket", type=float, default=100_000.0, help="flow bucket notional in USDT")
    ms.set_defaults(fn=cmd_micro_study)

    fast = sub.add_parser("fast", help="fast crypto strategy: backtest on recorded data, paper on live data")
    fsub = fast.add_subparsers(dest="sub", required=True)
    for name, fn, text in (
        ("backtest", cmd_fast_backtest, "replay a recording through the matching engine (fees, latency, queue)"),
        ("paper", cmd_fast_paper, "live Binance data, simulated fills; paper only"),
    ):
        fp = fsub.add_parser(name, help=text)
        fp.add_argument("symbol", help="Binance USD-M perpetual, e.g. BTCUSDT")
        fp.add_argument("--size", type=float, default=0.001, help="trade size in base units (default 0.001)")
        fp.add_argument("--threshold", type=float, default=0.6, help="|book imbalance| to enter (default 0.6)")
        fp.add_argument("--hold", type=float, default=5.0, help="seconds to hold (default 5)")
        fp.add_argument("--max-loss", type=float, default=100.0, help="stop after losing this much (quote)")
        fp.add_argument("--capital", type=float, default=10_000.0, help="simulated account (USDT)")
        fp.add_argument("--fee-bps", type=float, default=5.0, help="taker fee per side, bps (default 5)")
        fp.add_argument("--maker-bps", type=float, default=2.0, help="maker fee per side, bps (default 2)")
        fp.set_defaults(fn=fn)
        if name == "backtest":
            fp.add_argument("--catalog", help="catalog directory (default: <data>/market/catalog)")
            fp.add_argument("--start", help="UTC start")
            fp.add_argument("--end", help="UTC end")
            fp.add_argument("--latency-ms", type=float, default=50.0, help="order latency (default 50 ms)")
            fp.add_argument("--holdout", action="store_true", help="the one-time test on the reserved holdout data")
            fp.add_argument("--again", metavar="REASON", help="repeat a holdout test; the reason is recorded")
        else:
            fp.add_argument("--check-only", action="store_true", help="validate the setup without connecting")

    rel = fsub.add_parser("release", help="turn the holdout into research data and start a new holdout (logged)")
    rel.add_argument("symbol", help="Binance USD-M perpetual, e.g. BTCUSDT")
    rel.add_argument("--reason", required=True, help="recorded with the new boundary")
    rel.add_argument("--catalog", help="catalog directory (default: <data>/market/catalog)")
    rel.set_defaults(fn=cmd_fast_release)

    runtime = sub.add_parser("runtime", help="cold backup, upgrade preflight and broker-attested code migration")
    runtime.add_argument("action", choices=["backup", "preflight", "upgrade", "enable-gold"])
    runtime.add_argument("--destination", type=Path)
    runtime.add_argument("--evidence", type=Path)
    runtime.add_argument("--reason", default="")
    runtime.set_defaults(fn=cmd_runtime)

    control = sub.add_parser("control", help="inspect, approve or renew local paper authority")
    control.add_argument("action", choices=["status", "approve", "renew"])
    control.add_argument("--request")
    control.add_argument("--mode", choices=["autonomous", "supervised", "manual"])
    control.add_argument(
        "--hours", type=float, default=None, help="lease length (default: standing if autonomous, else 24 hours)"
    )
    control.add_argument(
        "--target", action="append", help="manual portfolio weight, e.g. SPY=0.2; unspecified stocks target zero"
    )
    control.set_defaults(fn=cmd_control)

    sub.add_parser(
        "paper-report", help="paper results, costs and implementation shortfall from the journal"
    ).set_defaults(fn=cmd_paper_report)
    k = sub.add_parser("kill", help="kill switch: stop all new orders now")
    k.add_argument("--flatten", action="store_true", help="also exit every position at the next open")
    k.add_argument("--reason", default="manual")
    k.set_defaults(fn=cmd_kill)
    sub.add_parser("resume", help="clear the kill switch").set_defaults(fn=cmd_resume)
    rc = sub.add_parser("recover", help="show or clear a hard halt (breaker, account guard, mismatch); audited")
    rc.add_argument("--reason", default="", help="what you checked and why trading may resume (required to clear)")
    rc.add_argument(
        "--adopt-broker", action="store_true", help="for a mismatch: take the broker's positions as the ledger's truth"
    )
    rc.add_argument(
        "--dispose-order",
        action="append",
        metavar="CLIENT_ORDER_ID",
        help="an unconfirmed journaled order the broker's records show never executed (needs --evidence)",
    )
    rc.add_argument("--evidence", help="what IBKR's order/trade history shows for the disposed order(s); recorded")
    rc.set_defaults(fn=cmd_recover)
    hl = sub.add_parser("health", help="exit 0 if the agents' heartbeat is fresh (for Docker)")
    hl.add_argument("--max-age", type=int, default=180)
    hl.add_argument("--ready", action="store_true", help="also require no hard halt, kill switch or block (exit 2)")
    hl.set_defaults(fn=cmd_health)
    sub.add_parser("doctor", help="pre-flight check of the whole deployment").set_defaults(fn=cmd_doctor)
    sub.add_parser("notify-test", help="send a test alert").set_defaults(fn=cmd_notify_test)

    sub.add_parser("signals", help="list available signals").set_defaults(fn=None)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "app":
        from lab.app import serve

        return serve(a)
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
    if a.cmd in ("paper", "backtest", "null-test", "pbo", "walk-forward", "holdout"):
        from lab.risk_profiles import profile_bindings

        for note in profile_bindings(cfg):
            print(f"note: {note}", file=sys.stderr)
    try:
        return a.fn(cfg, a) or 0
    except (data.DataError, ConfigError, BorrowUnavailable, experiments.HoldoutUsed) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
