"""lab: command-line interface for the trading lab."""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path

import pandas as pd

from lab import fast_research
from lab.config import ConfigError, load_config


def cmd_record(cfg, a):
    from lab.record import build_node, recover_stream, saved

    out = Path(a.out) if a.out else cfg.data_dir / "market"
    if a.status:
        kinds = saved(out)
        if not kinds:
            raise SystemExit(f"nothing saved in {out / 'catalog'} yet (data is saved every hour and at a clean stop)")
        for kind, instruments in kinds.items():
            print(f"{kind:20} " + "  ".join(f"{i}: {n} file(s)" for i, n in instruments.items()))
        return 0
    if not a.check_only:
        # Before this run's writer starts: what a killed earlier run left unsaved is otherwise never promoted.
        # Binance also takes the shared stream directory used before there was one per venue.
        streams = [out / "stream" / a.venue] + ([out / "stream"] if a.venue == "binance" else [])
        for run in (r for stream in streams for r in recover_stream(stream, out / "catalog")):
            size = f"{run['files']} file(s), {run['bytes'] / 1e6:.1f} MB"
            if "error" in run:
                print(f"could not recover {run['run']} ({size}), left in place: {run['error']}")
            else:
                print(f"recovered {run['run']} into the catalog ({size})")
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
    from lab.study_time import MAX_BOOK_GAP_NS

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
        "max_book_gap_ms": MAX_BOOK_GAP_NS // 1_000_000,
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


def cmd_survey(cfg, a):
    from urllib.error import URLError

    from lab.survey import survey

    try:
        table = survey(a.venue, a.samples, a.interval, a.maker_bps)
    except (URLError, TimeoutError) as e:
        raise SystemExit(f"could not reach {a.venue}: {e}") from e
    table = table[table["turnover_musd"] >= a.min_turnover].head(a.top)
    if table.empty:
        raise SystemExit("no perpetual passed the turnover filter")
    print(
        f"{a.venue} USDT perpetuals, {a.samples} sample(s) {a.interval:g} s apart, 24 h turnover >= {a.min_turnover:g} M USDT"
    )
    print(table.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print(
        f"room_bps: median spread minus a maker round trip (2 x {a.maker_bps:g} bps). Positive room is necessary, not"
        " sufficient: `lab maker-study` measures what fills give back to the moves that follow them."
    )
    picks = " ".join(table.loc[table["room_bps"] > 0, "symbol"].head(10))
    if picks:
        var = "LAB_RECORD_SYMBOLS" if a.venue == "binance" else "LAB_RECORD_BYBIT_SYMBOLS"
        print(f'to record the widest: {var}="{picks}" in .env, then docker compose up -d')
    return 0


def cmd_maker_study(cfg, a):
    from lab.maker_study import INFERENCE_LAGS, INFERENCE_METHOD, MIN_INFERENCE_DAYS, MakerSettings, study
    from lab.persistence import nullable_numbers
    from lab.record import perpetual_ids
    from lab.study_time import MAX_BOOK_GAP_NS

    catalog = Path(a.catalog) if a.catalog else cfg.data_dir / "market" / "catalog"
    if not catalog.exists():
        raise SystemExit(f"no recorded market data at {catalog}; run `lab record` first")
    try:
        iid = str(perpetual_ids([a.symbol], a.venue)[0])
    except ValueError as e:
        raise SystemExit(str(e)) from e
    start, end = (int(pd.Timestamp(x, tz="UTC").value) if x else None for x in (a.start, a.end))
    try:
        start, end = fast_research.research_window(cfg.results_dir, str(catalog), iid, start, end)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    settings = MakerSettings(
        every_ms=a.every_ms,
        latency_ms=a.latency_ms,
        horizons_s=tuple(a.horizons),
        maker_fee_bps=a.maker_bps,
        improve_ticks=a.improve,
    )
    try:
        fills, mids, table = study(str(catalog), iid, settings, start, end)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    if not len(mids[0]):
        raise SystemExit(f"no order-book data for {iid} in {catalog} before its holdout")
    params = {
        "every_ms": a.every_ms,
        "latency_ms": a.latency_ms,
        "horizons_s": list(a.horizons),
        "maker_bps": a.maker_bps,
        "improve": a.improve,
        "max_book_gap_ms": MAX_BOOK_GAP_NS // 1_000_000,
        "inference_method": INFERENCE_METHOD,
        "inference_lags": INFERENCE_LAGS,
        "minimum_inference_days": MIN_INFERENCE_DAYS,
        "midnight_rule": "same-utc-day",
    }
    n = fast_research.register_trial(
        cfg.results_dir, "maker-study", iid, params, (start, end),
        {"net_bps": {f"{r.group}@{r.horizon_s:g}s": r.net_bps for r in table.itertuples() if r.net_bps == r.net_bps},
         "uncertainty_report": nullable_numbers(table.to_dict("records"))},
    )  # fmt: skip
    first, last = (pd.Timestamp(mids[0][i], tz="UTC") for i in (0, -1))
    print(f"{iid}: {len(fills):,} passive fills, {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC")
    print(
        f"research data only (holdout from {pd.Timestamp(end + 1, tz='UTC'):%Y-%m-%d %H:%M} UTC); study settings tried: {n}"
    )
    print(table.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(
        "at_fill: half spread captured against the mid. markout: the maker's gain h seconds later, net: after the"
        f" {a.maker_bps:g} bps maker fee. t: {INFERENCE_METHOD}, requires {MIN_INFERENCE_DAYS} eligible UTC days;"
        " inference_net_bps and se_bps use same-day windows; t_naive is descriptive only."
        " effective_days measures concentration/dependence, not a count of independent days."
        " group: book imbalance when the order was placed, in its favour, neutral or against; unknowns enter all only."
    )
    return 0


def _fast_config(cfg, a, *, report=None):
    from lab.fast import ImbalanceTakerConfig
    from lab.maker import PassiveMakerConfig
    from lab.record import perpetual_ids

    try:
        iid = str(perpetual_ids([a.symbol])[0])
        if a.strategy == "maker":
            return PassiveMakerConfig(
                instrument_id=iid,
                quote_size=a.size,
                max_inventory=a.max_inventory,
                improve_ticks=a.improve,
                min_favour=a.min_favour,
                max_hold_s=a.max_hold,
                report=report,
            )
        return ImbalanceTakerConfig(
            instrument_id=iid, trade_size=a.size, threshold=a.threshold, hold_s=a.hold, report=report
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e


def _strategy_params(a) -> dict:
    """The strategy's own settings, as they are fingerprinted in the trial log and the holdout ledger."""
    if a.strategy == "maker":
        return {
            "strategy": "passive_maker", "size": a.size, "max_inventory": a.max_inventory, "improve": a.improve,
            "min_favour": a.min_favour, "max_hold_s": a.max_hold,
        }  # fmt: skip
    return {"strategy": "imbalance_taker", "size": a.size, "threshold": a.threshold, "hold_s": a.hold}


STRATEGY_NAMES = {"taker": "imbalance taker", "maker": "passive maker"}


def cmd_fast_backtest(cfg, a):
    from lab.fast import backtest

    catalog = Path(a.catalog) if a.catalog else cfg.data_dir / "market" / "catalog"
    if not catalog.exists():
        raise SystemExit(f"no recorded market data at {catalog}; run `lab record` first")
    start, end = (int(pd.Timestamp(x, tz="UTC").value) if x else None for x in (a.start, a.end))
    config = _fast_config(cfg, a)
    iid = config.instrument_id
    params = {
        **_strategy_params(a),
        "max_loss": a.max_loss, "max_position": a.max_position, "capital": a.capital, "taker_bps": a.fee_bps, "maker_bps": a.maker_bps,
        "latency_ms": a.latency_ms,
    }  # fmt: skip
    try:
        if a.holdout:
            if a.start or a.end:
                raise SystemExit("--holdout runs on the whole holdout; --start/--end don't apply")
            start = fast_research.boundary(cfg.results_dir, str(catalog), iid)
            end = fast_research.last_recorded(str(catalog), iid)
            evaluation = fast_research.holdout_run(cfg.results_dir, iid, params, start, a.again or "", holdout_end=end)
        else:
            if a.again:
                raise SystemExit("--again only applies with --holdout")
            start, end = fast_research.research_window(cfg.results_dir, str(catalog), iid, start, end)
            evaluation = nullcontext(None)
        with evaluation as attempt:
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
            result = {
                k: r[k] for k in ("entries", "pnl", "commissions", "round_trips", "mean_trip", "t_stat", "stopped")
            }
            result["return_stats"] = r["return_stats"]
            if attempt is not None:
                attempt.result = result
            n = fast_research.register_trial(
                cfg.results_dir, "fast-backtest", iid, params, (r["start_ns"], r["end_ns"]), result, holdout=a.holdout
            )
    except ValueError as e:
        raise SystemExit(str(e)) from e
    first, last = (pd.Timestamp(r[k], tz="UTC") for k in ("start_ns", "end_ns"))
    if attempt is not None and attempt.reused:
        print(f"HOLDOUT REUSE (reason recorded): {a.again}")
    print(f"{iid} {STRATEGY_NAMES[a.strategy]}, {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC")
    print(
        f"  {'HOLDOUT test (recorded)' if a.holdout else 'research data'}; configurations tried on research data: {n}"
    )
    print(
        f"  entries {r['entries']}, fills {r['fills']}, commissions {r['commissions']:.4f}, "
        f"P&L after costs {r['pnl']:+.4f} (quote currency)"
    )
    print(f"  round trips {r['round_trips']}, mean {r['mean_trip']:+.5f} each, t-stat {r['t_stat']:+.2f}")
    if not a.holdout:
        score, reason = fast_research.fast_deflated_sharpe(
            cfg.results_dir, iid, (r["start_ns"], r["end_ns"]), r["return_stats"]
        )
        if score is None:
            print(f"  deflated Sharpe unavailable: {reason}")
        else:
            print(f"  deflated Sharpe {score:.3f} (IID diagnostic, {n} research configurations)")
        print(
            "  daily net equity returns; trial correlation and serial dependence are not adjusted: no gate pass implied"
        )
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
        f"fast paper node ready: {a.symbol.upper()} {STRATEGY_NAMES[a.strategy]} on live Binance data, simulated fills "
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


def cmd_carry_fetch(cfg, a):
    from urllib.error import HTTPError, URLError

    from lab import carry_data

    root = cfg.data_dir / "carry"
    since, now = pd.Timestamp(a.since, tz="UTC"), pd.Timestamp.now(tz="UTC")
    venues = a.venues or (["binance"] if a.all else list(carry_data.VENUES))
    if a.all:
        try:
            symbols = carry_data.universe()
        except (URLError, TimeoutError) as e:
            raise SystemExit(f"could not reach binance: {e}") from e
        print(f"{len(symbols)} USDT pairs trade as both a Binance perpetual and spot pair today")
    else:
        symbols = [s.upper() for s in a.symbols]
    for sym in symbols:
        for (venue, market), kinds in carry_data.SERIES.items():
            if venue not in venues:
                continue
            for kind in kinds:
                name = f"{sym} {venue} {market} {kind}"
                try:
                    added, total = carry_data.update(root, venue, market, kind, sym, since, now)
                except (HTTPError, ValueError) as e:  # e.g. no spot market for the symbol
                    print(f"{name:34} not available ({e}): skipped")
                    continue
                except (URLError, TimeoutError) as e:
                    raise SystemExit(f"could not reach {venue}: {e}") from e
                print(f"{name:34} +{added:,} rows, {total:,} cached")
    print(f"cached in {root}; next: lab carry study")
    return 0


def cmd_carry_study(cfg, a):
    from lab import carry_data
    from lab import carry_study as cs
    from lab.persistence import nullable_numbers

    root = cfg.data_dir / "carry"
    symbols = carry_data.cached_symbols(root) if a.all else [s.upper() for s in a.symbols]
    try:
        s = cs.CarrySettings(
            mode=a.mode, lookback_h=a.lookback_h, entry_apr=a.entry_apr, exit_apr=a.exit_apr,
            rebalance_h=a.rebalance_h, perp_fee_bps=a.fee_bps, spot_fee_bps=a.spot_fee_bps, slippage_bps=a.slippage_bps,
            top=a.top, min_turnover_m=a.min_turnover,
        )  # fmt: skip
        b = cs.boundary(cfg.results_dir, root)
        params = cs.params_of(s, symbols)
        key = f"carry-{a.mode}"
        if a.holdout:
            end = cs._last_cached(root)
            evaluation = fast_research.holdout_run(
                cfg.results_dir,
                key,
                params,
                int(b.value),
                a.again or "",
                holdout_end=int(end.value) if end is not None else None,
                partition=cs.PARTITION,
            )
        elif a.again:
            raise SystemExit("--again only applies with --holdout")
        else:
            end, evaluation = None, nullcontext(None)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    try:
        with evaluation as attempt:
            pnl, missing = cs.run(root, symbols, s, b, holdout=a.holdout, end=end)
            if not pnl:
                raise ValueError(
                    f"no carry data for {' '.join(symbols)} {'after' if a.holdout else 'before'} {b:%Y-%m-%d}"
                )
            st = cs.summary(pnl, s.top)
            result = nullable_numbers({k: st[k] for k in ("days", "apr", "sharpe", "sr_daily", "t_nw", "max_dd")})
            if attempt is not None:
                attempt.result = result
            first, last = min(v.index[0] for v in pnl.values()), max(v.index[-1] for v in pnl.values())
            n = fast_research.register_trial(
                cfg.results_dir,
                "carry-study",
                key,
                params,
                (int(first.value), int(last.value)),
                result,
                holdout=a.holdout,
            )
    except ValueError as e:
        raise SystemExit(str(e)) from e
    trade = {"cash": "long Binance spot / short Binance perpetual", "cross": "short the higher-funding perpetual, long the other (Binance vs Bybit)"}[a.mode]  # fmt: skip
    if attempt is not None and attempt.reused:
        print(f"HOLDOUT REUSE (reason recorded): {a.again}")
    who = f"top {s.top} of {len(pnl)} symbol(s) by funding" if s.top else f"{len(pnl)} symbol(s)"
    if s.min_turnover_m:
        who += f", perpetual turnover >= {s.min_turnover_m:g} M USDT a day"
    print(f"carry {a.mode}: {trade}, {who}, {st['start']} .. {st['end']} UTC")
    if a.holdout:
        psr = cs.probabilistic_sharpe(st["sr_daily"], st["days"], st["skew"], st["kurt"])
        print(f"  HOLDOUT test from {b:%Y-%m-%d} (recorded); P(Sharpe > 0) {psr:.2f}")
    else:
        srs = cs.trial_sharpes(cfg.results_dir, "carry-study", key)
        dsr = cs.probabilistic_sharpe(st["sr_daily"], st["days"], st["skew"], st["kurt"], cs.expected_max_sharpe(srs))
        print(
            f"  research data only (holdout from {b:%Y-%m-%d}); configurations tried: {n}; "
            f"deflated Sharpe {dsr:.2f} (IID diagnostic after {n} tries; dependence is not adjusted)"
        )
    if missing:
        print(
            f"  no data, left out: {' '.join(missing[:20])}"
            + (f" and {len(missing) - 20} more" if len(missing) > 20 else "")
        )
    print(
        f"  net {st['apr']:+.2f}% a year (funding {st['funding_apr']:+.2f}, basis {st['basis_apr']:+.2f}, "
        f"costs -{st['cost_apr']:.2f}), vol {st['vol']:.2f}%, Sharpe {st['sharpe']:+.2f}, "
        f"t (Newey-West) {st['t_nw']:+.2f}, max drawdown {st['max_dd']:.2f}%, in market {st['in_market']:.0f}%"
    )
    if a.mode == "cash" and not a.holdout and not s.top:
        always = cs.CarrySettings(**{**cs.asdict(s), "entry_apr": -1e9, "exit_apr": -1e9})
        base = cs.summary(cs.run(root, symbols, always, b)[0])
        print(f"  always in the trade, for comparison: net {base['apr']:+.2f}% a year, Sharpe {base['sharpe']:+.2f}")
    table = cs.per_symbol(pnl)
    if s.top:  # a universe: the symbols that were held, best and worst
        table = table[table["trades"] > 0].sort_values("net_bps", ascending=False)
        if len(table) > 20:
            print(f"  {len(table)} symbols were held; the 10 best and 10 worst:")
            table = pd.concat([table.head(10), table.tail(10)])
    print(table.to_string(index=False, float_format=lambda x: f"{x:.1f}"))
    share = f"1/{s.top} of the notional per slot" if s.top else "equal notional per symbol"
    print(
        f"Returns per unit of notional per leg, {share}, after taker fees and slippage on both"
        " legs. Deflated Sharpe is a research diagnostic; trial correlation and serial dependence need separate"
        " qualification before any gate can pass."
    )
    return 0


def cmd_carry_release(cfg, a):
    from lab import carry_study as cs

    try:
        old, new = cs.release(cfg.results_dir, cfg.data_dir / "carry", a.reason)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    print(
        f"carry data {old:%Y-%m-%d %H:%M} .. {new:%Y-%m-%d %H:%M} UTC is now research data; the new holdout is "
        "everything fetched from here on"
    )
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

    sv = sub.add_parser("survey", help="which perpetuals quote a spread wider than a maker round trip? (live, public)")
    sv.add_argument("--venue", choices=VENUES, default="binance")
    sv.add_argument("--samples", type=int, default=30, help="quote snapshots (default 30)")
    sv.add_argument("--interval", type=float, default=10.0, help="seconds between snapshots (default 10)")
    sv.add_argument("--maker-bps", type=float, default=2.0, help="maker fee per side in bps (default 2)")
    sv.add_argument("--min-turnover", type=float, default=20.0, help="minimum 24 h turnover, M USDT (default 20)")
    sv.add_argument("--top", type=int, default=30, help="rows to show (default 30)")
    sv.set_defaults(fn=cmd_survey)

    mk = sub.add_parser("maker-study", help="what do passive fills earn after the moves that follow them?")
    mk.add_argument("symbol", help="a recorded perpetual, e.g. BTCUSDT")
    mk.add_argument("--venue", choices=VENUES, default="binance", help="venue of the symbol (default binance)")
    mk.add_argument("--catalog", help="catalog directory (default: <data>/market/catalog)")
    mk.add_argument("--start", help="UTC start, e.g. 2026-11-01")
    mk.add_argument("--end", help="UTC end")
    mk.add_argument("--every-ms", type=int, default=100, help="requote clock (default 100 ms)")
    mk.add_argument("--latency-ms", type=int, default=50, help="from placing an order to it resting (default 50)")
    mk.add_argument("--horizons", type=float, nargs="+", default=[1.0, 5.0, 30.0, 60.0], help="seconds after fill")
    mk.add_argument("--maker-bps", type=float, default=2.0, help="maker fee per side in bps (default 2)")
    mk.add_argument("--improve", type=int, default=0, help="quote this many ticks inside the spread (default 0: join)")
    mk.set_defaults(fn=cmd_maker_study)

    fast = sub.add_parser("fast", help="fast crypto strategy: backtest on recorded data, paper on live data")
    fsub = fast.add_subparsers(dest="sub", required=True)
    for name, fn, text in (
        ("backtest", cmd_fast_backtest, "replay a recording through the matching engine (fees, latency, queue)"),
        ("paper", cmd_fast_paper, "live Binance data, simulated fills; paper only"),
    ):
        fp = fsub.add_parser(name, help=text)
        fp.add_argument("symbol", help="Binance USD-M perpetual, e.g. BTCUSDT")
        fp.add_argument(
            "--strategy", choices=sorted(STRATEGY_NAMES), default="taker", help="imbalance taker or passive maker"
        )
        fp.add_argument("--size", type=float, default=0.001, help="trade / quote size in base units (default 0.001)")
        fp.add_argument("--threshold", type=float, default=0.6, help="taker: |book imbalance| to enter (default 0.6)")
        fp.add_argument("--hold", type=float, default=5.0, help="taker: seconds to hold (default 5)")
        fp.add_argument("--max-inventory", type=float, help="maker: position cap, base units (default 3 x --size)")
        fp.add_argument("--improve", type=int, default=0, help="maker: ticks inside the spread (default 0: join)")
        fp.add_argument(
            "--min-favour", type=float, default=-1.0, help="maker: quote a side only while side x imbalance >= this"
        )
        fp.add_argument("--max-hold", type=float, default=60.0, help="maker: exit inventory older than this, s (60)")
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

    from lab.carry_study import DEFAULT_SYMBOLS as CARRY_SYMBOLS
    from lab.carry_study import MODES as CARRY_MODES

    carry = sub.add_parser("carry", help="funding-carry research on hourly history (public REST data)")
    csub = carry.add_subparsers(dest="sub", required=True)
    cf = csub.add_parser("fetch", help="download or update funding and hourly closes into <data>/carry")
    cf.add_argument("--symbols", nargs="+", default=list(CARRY_SYMBOLS), help="USDT pairs")
    cf.add_argument("--since", default="2023-01-01", help="UTC start of the first download (default 2023-01-01)")
    cf.add_argument("--all", action="store_true", help="every pair with a Binance perpetual and spot market today")
    cf.add_argument("--venues", nargs="+", choices=VENUES, help="default: all (binance with --all)")
    cf.set_defaults(fn=cmd_carry_fetch)
    cst = csub.add_parser("study", help="does a carry trade pay after costs? (holdout-guarded, logged)")
    cst.add_argument(
        "--mode", choices=CARRY_MODES, default="cash", help="cash: spot vs perpetual; cross: Binance vs Bybit"
    )
    cst.add_argument("--symbols", nargs="+", default=list(CARRY_SYMBOLS), help="USDT pairs")
    cst.add_argument("--lookback-h", type=int, default=72, help="hours of funding behind the signal (default 72)")
    cst.add_argument("--entry-apr", type=float, default=10.0, help="open above this funding, %% a year (default 10)")
    cst.add_argument("--exit-apr", type=float, default=0.0, help="close below this funding, %% a year (default 0)")
    cst.add_argument("--rebalance-h", type=int, default=8, help="decide every N hours from 00:00 UTC (default 8)")
    cst.add_argument("--fee-bps", type=float, default=5.0, help="perpetual taker fee per side, bps (default 5)")
    cst.add_argument("--spot-fee-bps", type=float, default=10.0, help="spot taker fee per side, bps (default 10)")
    cst.add_argument("--slippage-bps", type=float, default=1.0, help="per leg and side, bps (default 1)")
    cst.add_argument("--all", action="store_true", help="every symbol fetched (instead of --symbols)")
    cst.add_argument("--top", type=int, default=0, help="cash: hold only the K highest-funding symbols (default: all)")
    cst.add_argument(
        "--min-turnover", type=float, default=0.0, help="open only where the perpetual traded >= M USDT a day (30 d)"
    )
    cst.add_argument("--holdout", action="store_true", help="the one-time test on the reserved holdout data")
    cst.add_argument("--again", metavar="REASON", help="repeat a holdout test; the reason is recorded")
    cst.set_defaults(fn=cmd_carry_study)
    crl = csub.add_parser("release", help="turn the carry holdout into research data; later data is the new holdout")
    crl.add_argument("--reason", required=True, help="recorded with the new boundary")
    crl.set_defaults(fn=cmd_carry_release)

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
