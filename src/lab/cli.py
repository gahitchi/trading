"""lab: command-line interface for the trading lab."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from lab import fast_research
from lab.config import ConfigError, load_config


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
        node, _ = build_node(a.symbols, out, venue=a.venue)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    symbols = " ".join(s.upper() for s in a.symbols)
    market = {"binance": "Binance USD-M", "bybit": "Bybit linear"}[a.venue]
    print(f"market-data recorder ready: {market} {symbols} (order book, trades, mark price, funding) -> {out}")
    if a.check_only:
        node.dispose()
        print("check only: configuration is valid; not connecting.")
        return 0
    print(f"connecting to {market} public market data... (Ctrl+C to stop)")
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


def cmd_monitor(cfg, a):
    from lab.monitor import serve

    try:
        serve(Path(a.out) if a.out else cfg.data_dir / "market", a.host, a.port, results=cfg.results_dir)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    return 0


def cmd_micro_study(cfg, a):
    from lab.micro_study import StudySettings, study
    from lab.record import perpetual_ids

    catalog = Path(a.catalog) if a.catalog else cfg.data_dir / "market" / "catalog"
    if not catalog.exists():
        raise SystemExit(f"no recorded market data at {catalog}; run `lab record` first")
    try:
        iid = str(perpetual_ids([a.symbol], a.venue)[0])
        leader = str(perpetual_ids([a.leader], a.leader_venue)[0]) if a.leader else None
    except ValueError as e:
        raise SystemExit(str(e)) from e
    if leader == iid:
        raise SystemExit("--leader must be another symbol or venue")
    start, end = (int(pd.Timestamp(x, tz="UTC").value) if x else None for x in (a.start, a.end))
    try:
        start, end = fast_research.research_window(cfg.results_dir, str(catalog), iid, start, end)
        if leader:  # the leader's own holdout stays unseen too
            end = min(end, fast_research.research_window(cfg.results_dir, str(catalog), leader, start, None)[1])
    except ValueError as e:
        raise SystemExit(str(e)) from e
    settings = StudySettings(
        every_ms=a.every_ms,
        horizons_s=tuple(a.horizons),
        taker_fee_bps=a.fee_bps,
        flow_bucket_notional=a.bucket,
        hawkes_baseline_per_s=a.hawkes_mu,
        lead_lag_max_interval_s=a.lead_lag_interval,
    )
    samples, table = study(str(catalog), iid, settings, start, end, leader)
    if samples.empty:
        raise SystemExit(f"no order-book data for {iid} in {catalog} before its holdout")
    first, last = (pd.Timestamp(samples["ts"].iloc[i], tz="UTC") for i in (0, -1))
    params = {
        "every_ms": a.every_ms,
        "horizons_s": list(a.horizons),
        "fee_bps": a.fee_bps,
        "bucket": a.bucket,
        "hawkes_mu": a.hawkes_mu,
        "leader": leader,
        "lead_lag_interval": a.lead_lag_interval,
    }
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


def _fast_config(cfg, a, *, report=None):
    from lab.fast import ImbalanceTakerConfig
    from lab.record import perpetual_ids

    try:
        return ImbalanceTakerConfig(
            instrument_id=str(perpetual_ids([a.symbol])[0]),
            trade_size=a.size,
            threshold=a.threshold,
            hold_s=a.hold,
            report=report,
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
        "max_loss": a.max_loss, "max_position": a.max_position, "capital": a.capital, "taker_bps": a.fee_bps, "maker_bps": a.maker_bps,
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
            max_loss=a.max_loss,
            max_position=a.max_position,
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

    risk_dir = cfg.results_dir / "fast"
    try:
        node, _ = build_paper_node(
            _fast_config(cfg, a, report=str(risk_dir / "paper.json")),
            capital=a.capital, taker_bps=a.fee_bps, maker_bps=a.maker_bps,
            max_loss=a.max_loss, max_position=a.max_position, kill_file=str(cfg.kill_path),
            state_path=str(risk_dir / "risk-state.json"), audit_path=str(risk_dir / "risk-audit.jsonl"),
        )  # fmt: skip
    except ValueError as e:
        raise SystemExit(str(e)) from e
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


def cmd_kill(cfg, a):
    from lab.notify import Notifier
    from lab.safety import set_kill_switch

    state = set_kill_switch(cfg.kill_path, "flatten" if a.flatten else "halt", a.reason)
    msg = (
        "KILL SWITCH: no new orders, open orders are cancelled"
        + ("; all positions exit" if a.flatten else "")
        + f". Reason: {a.reason}"
    )
    print(msg)
    Notifier().send_now(msg)
    print(f"set {cfg.kill_path.relative_to(cfg.root)} at {state['at']}. Undo with: lab resume")


def cmd_resume(cfg, a):
    from lab.notify import Notifier
    from lab.safety import clear_kill_switch

    if clear_kill_switch(cfg.kill_path):
        print("kill switch cleared; new orders are allowed again")
        Notifier().send_now("kill switch cleared; new orders are allowed again")
    else:
        print("kill switch was not set")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lab", description="Fast crypto trading lab on NautilusTrader (paper only).")
    p.add_argument("--config", help="path to lab.toml (default: search upwards from here)")
    sub = p.add_subparsers(dest="cmd", required=True)

    from lab.record import DEFAULT_SYMBOLS, VENUES

    rec = sub.add_parser("record", help="record crypto order books, trades, mark prices and funding (data only)")
    rec.add_argument("--venue", choices=VENUES, default="binance", help="exchange; run one recorder per venue")
    rec.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS), help="USDT-margined perpetuals")
    rec.add_argument("--out", help="output directory (default: <data>/market)")
    rec.add_argument("--check-only", action="store_true", help="validate the setup without connecting")
    rec.add_argument("--status", action="store_true", help="list what has been saved to the catalog, then exit")
    rec.set_defaults(fn=cmd_record)

    mon = sub.add_parser("monitor", help="read-only dashboard of the recorder's health and recorded data")
    mon.add_argument("--out", help="the recorder's output directory (default: <data>/market)")
    mon.add_argument("--port", type=int, default=8766)
    mon.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    mon.set_defaults(fn=cmd_monitor)

    ms = sub.add_parser("micro-study", help="do order-book and trade-flow features predict moves beyond costs?")
    ms.add_argument("symbol", help="a recorded perpetual, e.g. BTCUSDT")
    ms.add_argument("--venue", choices=VENUES, default="binance", help="venue of the symbol (default binance)")
    ms.add_argument("--catalog", help="catalog directory (default: <data>/market/catalog)")
    ms.add_argument("--start", help="UTC start, e.g. 2026-11-01")
    ms.add_argument("--end", help="UTC end")
    ms.add_argument("--every-ms", type=int, default=100, help="sampling clock (default 100 ms)")
    ms.add_argument("--horizons", type=float, nargs="+", default=[1.0, 5.0, 30.0], help="seconds ahead")
    ms.add_argument("--fee-bps", type=float, default=5.0, help="taker fee per side in bps (default 5)")
    ms.add_argument("--bucket", type=float, default=100_000.0, help="flow bucket notional in USDT")
    ms.add_argument("--hawkes-mu", type=float, default=1.0, help="Hawkes baseline intensity, trades/s (default 1)")
    ms.add_argument("--leader", help="lead-lag: a recorded symbol whose moves may lead, e.g. BTCUSDT")
    ms.add_argument("--leader-venue", choices=VENUES, default="binance", help="venue of --leader (default binance)")
    ms.add_argument(
        "--lead-lag-interval", type=float, default=0.05, help="longest gap (s) between mids that is a return (0.05)"
    )
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
        fp.add_argument(
            "--max-loss", type=float, default=100.0, help="daily loss cap (quote): flatten, no new risk until 00:00 UTC"
        )
        fp.add_argument("--max-position", type=float, help="position and order notional cap (default: --capital)")
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

    k = sub.add_parser("kill", help="kill switch: stop all new orders now")
    k.add_argument("--flatten", action="store_true", help="also exit every position")
    k.add_argument("--reason", default="manual")
    k.set_defaults(fn=cmd_kill)
    sub.add_parser("resume", help="clear the kill switch").set_defaults(fn=cmd_resume)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    pd.set_option("display.width", 160)
    return a.fn(cfg, a) or 0


if __name__ == "__main__":
    sys.exit(main())
