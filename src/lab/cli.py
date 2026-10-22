"""lab: command-line interface for the trading lab."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import pandas as pd

from lab import data, experiments
from lab.config import ConfigError, LabConfig, Period, load_config
from lab.signals.registry import SignalNotFound, available_research, resolve
from lab.signals.builtin import BUILTIN


def _period(cfg: LabConfig, a) -> Period:
    if a.start or a.end:
        start = date.fromisoformat(a.start) if a.start else cfg.period(a.period).start
        end = date.fromisoformat(a.end) if a.end else None
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
    tickers = [t.upper() for t in given] if given else list(cfg.universe)
    have = data.manifest(cfg)
    missing = [t for t in tickers if t not in have]
    if missing:
        raise SystemExit(f"no data for {missing}; run: lab data fetch {' '.join(missing)}")
    return tickers


def _print_table(comp: experiments.Comparison) -> None:
    df = comp.table().copy()
    for c in ("total_return", "cagr", "max_drawdown", "avg_exposure"):
        if c in df:
            df[c] = df[c].map(lambda v: f"{v:+.1%}" if c in ("total_return", "cagr") else f"{v:.0%}")
    for c in ("sharpe", "sharpe_edge"):
        if c in df:
            df[c] = df[c].map(lambda v: f"{v:+.2f}" if c == "sharpe_edge" else f"{v:.2f}")
    print(f"\n{comp.period} {comp.start} .. {comp.end} | {len(comp.tickers)} stocks: {', '.join(comp.tickers)}")
    print(df.to_string(index=False))


# ----------------------------------------------------------------------------- data


def cmd_data_fetch(cfg, a):
    tickers = [t.upper() for t in a.tickers] or list(cfg.universe)
    start = date.fromisoformat(a.start) if a.start else cfg.warmup_start
    failed = []
    for t in tickers:
        try:
            df = data.fetch_yfinance(t, start)
            e = data.save(cfg, t, df, source="yahoo_finance", adjusted=True)
            print(f"{t:<6} {e['rows']:>5} days  {e['first']} .. {e['last']}")
        except data.DataError as err:
            failed.append(t)
            print(f"{t:<6} FAILED: {err}")
    return 1 if failed else 0


def cmd_data_import(cfg, a):
    df = data.import_csv(a.file, a.date_column)
    e = data.save(cfg, a.ticker, df, source=f"csv:{a.file}", adjusted=a.adjusted, note=a.note or "")
    print(f"{a.ticker.upper()}: {e['rows']} days {e['first']} .. {e['last']}")


def cmd_data_sample(cfg, a):
    e = data.save(cfg, "GOOG", data.sample_goog(), source="backtesting.py sample (real GOOG prices)",
                  adjusted=False, note="unadjusted; for offline pipeline checks")
    print(f"GOOG: {e['rows']} days {e['first']} .. {e['last']} (real prices, offline sample)")


def cmd_data_list(cfg, a):
    m = data.manifest(cfg)
    if not m:
        print("no data yet: lab data fetch")
        return
    for t, e in sorted(m.items()):
        print(f"{t:<6} {e['rows']:>5} days  {e['first']} .. {e['last']}  {e['source']}{'' if e['adjusted'] else ' (unadjusted)'}")


# ----------------------------------------------------------------------------- experiments


def _specs(cfg, names, sets, require_tradable=False):
    params = _params(sets)
    try:
        return [resolve(n, cfg.research_dir, params.get(n), require_tradable=require_tradable) for n in names]
    except (SignalNotFound, ValueError) as e:
        raise SystemExit(str(e)) from e


def cmd_backtest(cfg, a):
    if a.period == "holdout" and not (a.start or a.end):
        raise SystemExit("the holdout is guarded: use `lab holdout <signal>`")
    specs = _specs(cfg, a.signals, a.set)
    tickers = _tickers(cfg, a.tickers)
    comp, results = experiments.compare(cfg, specs, tickers, _period(cfg, a))
    _print_table(comp)
    for r in results:
        if r.signal_errors or r.rejections:
            print(f"  note: {r.label}: {r.signal_errors} signal errors, {r.rejections} rejected orders")
    out = experiments.save_run(cfg, "backtest", comp, results)
    print(f"\nsaved: {out.relative_to(cfg.root)}")


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
    nt = experiments.null_test(cfg, spec, tickers, period, a.samples, frames, observed_edge=comp.rows[1]["sharpe_edge"])
    print(f"observed Sharpe edge {nt['observed_sharpe_edge']:+.2f} | noise: mean {nt['null_mean']:+.2f}, "
          f"sd {nt['null_std']:.2f}, 95th pct {nt['null_p95']:+.2f} | p = {nt['p_value']:.3f}")
    verdict = "beats the noise floor" if nt["p_value"] <= cfg.research.max_p_value else "indistinguishable from noise"
    print(f"=> {verdict} (threshold p <= {cfg.research.max_p_value})")
    out = experiments.save_run(cfg, f"null-{spec.name}", comp, results, {"null_test": nt})
    print(f"saved: {out.relative_to(cfg.root)}")


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
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "sum": {"type": "integer"}},
              "required": ["ok", "sum"], "additionalProperties": False}
    try:
        out = p.complete_json(role="check", system="Answer exactly as asked.",
                              prompt="Return ok=true and sum = 17 + 25.", schema=schema)
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
                obj = {k: v for k, v in obj.items() if k in ("passed", "checks", "sharpe_edge", "survived")} | \
                      {"candidate": obj["rows"][1]}
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
    from lab.paper import build_node
    from lab.safety import SafetyError

    specs = _specs(cfg, a.signals, a.set, require_tradable=False)
    for s in specs:
        if s.path is not None:
            resolve(s.name, cfg.research_dir, s.params, require_tradable=True)
    tickers = [t.upper() for t in (a.tickers or cfg.universe)]
    try:
        node = build_node(cfg, specs, tickers)
    except (SafetyError, ValueError) as e:
        raise SystemExit(str(e)) from e
    print(f"paper node ready: {len(specs)} signal(s) x {len(tickers)} stocks, account {cfg.paper.account_id} "
          f"via {cfg.paper.host}:{cfg.paper.port}")
    if a.check_only:
        node.dispose()
        print("check only: configuration is valid; not connecting.")
        return
    print("connecting to Interactive Brokers... (Ctrl+C to stop)")
    try:
        node.run()
    finally:
        node.dispose()


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
    i.add_argument("--note")
    i.set_defaults(fn=cmd_data_import)
    d.add_parser("sample", help="store the offline real-data sample (GOOG 2004-2013)").set_defaults(fn=cmd_data_sample)
    d.add_parser("list", help="show stored data").set_defaults(fn=cmd_data_list)

    def period_args(x, default="train"):
        x.add_argument("--period", default=default, choices=["train", "test", "holdout"])
        x.add_argument("--start")
        x.add_argument("--end")
        x.add_argument("--tickers", nargs="+")
        x.add_argument("--set", action="append", help="override a parameter, e.g. sma_trend.fast=20")

    b = sub.add_parser("backtest", help="compare signals against buy-and-hold")
    b.add_argument("signals", nargs="*", default=["sma_trend", "rsi_reversion"])
    period_args(b)
    b.set_defaults(fn=cmd_backtest)

    n = sub.add_parser("null-test", help="test a signal against the bootstrap noise floor")
    n.add_argument("signal")
    n.add_argument("--samples", type=int, default=30)
    period_args(n)
    n.set_defaults(fn=cmd_null)

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
    pp.set_defaults(fn=cmd_paper)

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
    except (data.DataError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
