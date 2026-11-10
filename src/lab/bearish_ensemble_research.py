"""Pre-registered BTCUSDT bearish-ensemble replay: historical research, never execution.

The plan subcommand reads configuration/partition metadata only; no price
archives. Evaluation refuses a missing/mismatched research family, then
reserves an immutable trial BEFORE opening any market data. Historical
candles are restricted to the established 2026-05-08 protected boundary.
This OHLC study is descriptive, never an inferential or paper-trading gate.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from lab import crash_data, crash_study
from lab.bearish_ensemble import EnsembleSettings, RoundTripCosts
from lab.bearish_ensemble_replay import ReplayAssumptions, replay_research_frame
from lab.config import load_config
from lab.persistence import read_json
from lab.research_family import _digest, load_family
from lab.trials import _code_identity, code_provenance, trial_run

MODEL = "btc-bearish-ensemble-ohlc-v1"
SYMBOL = "BTCUSDT"
BOUNDARY = pd.Timestamp("2026-05-08T00:00:00Z")
COSTS = RoundTripCosts(5.0, 5.0, 2.0, 1.0)
SETTINGS = EnsembleSettings()
ASSUMPTIONS = ReplayAssumptions()
POLICY = "research-only-closed-minute-ohlc-short-preholdout-v1"


def _config(config_path: Path):
    """Metadata-only validation; never reads archive candles or funding."""
    cfg = load_config(config_path)
    partition = read_json(cfg.results_dir / "crash_partition.json")
    if (
        not isinstance(partition, dict)
        or partition.get("version") != 1
        or pd.Timestamp(partition.get("holdout_start")) != BOUNDARY
    ):
        raise ValueError("original BTCUSDT protected holdout boundary missing or changed")
    return cfg


def member(config_path: Path) -> dict:
    _config(config_path)
    params = {
        "model": MODEL,
        "policy": POLICY,
        "instrument": SYMBOL,
        "side": -1,
        "signal": asdict(SETTINGS),
        "cost_estimates": asdict(COSTS),
        "replay": asdict(ASSUMPTIONS),
        "source_policy": "utc-candle-end-before-cutoff-oi-quality-veto",
        "research_status": "retrospective-unqualified",
        "holdout_start": BOUNDARY.isoformat(),
    }
    return {
        "kind": "crash-study",
        "instrument": MODEL,
        "params": params,
        "window": [None, int(BOUNDARY.value) - 1],
    }


def proposed_plan(config_path: Path) -> dict:
    return {
        "name": "BTCUSDT short-only bearish ensemble OHLC v1",
        "rationale": (
            "New short-only research hypothesis. Existing BTC breakdown/retest failures "
            "remain counted and the historical research period was previously inspected. "
            "Exploratory pre-holdout OHLC replay cannot qualify inference or execution."
        ),
        "members": [member(config_path)],
    }


def _registered(results_dir: Path, expected: dict) -> None:
    family = load_family(results_dir)
    if family is None:
        raise ValueError("no research family registered; generate and register plan before evaluation")
    if _digest(expected) not in {_digest(item) for item in family["plan"]["members"]}:
        raise ValueError("bearish ensemble is not an exact member of the registered research family")
    if _code_identity(family["code"]) != _code_identity(code_provenance()):
        raise ValueError("research code/runtime differs from frozen registration")


def _load_preholdout(root: Path, kind: str, *, attempt) -> pd.DataFrame:
    # crash_data.load selects only archived periods ending by BOUNDARY,
    # BEFORE opening any parquet file (including monthly archives).
    source = crash_data.load(root, kind, SYMBOL, before=BOUNDARY)
    if source is None or source.empty:
        raise ValueError(f"missing pre-holdout {kind} archives")
    if "ts" not in source or source["ts"].isna().any():
        raise ValueError(f"invalid {kind} timestamp source")
    ts = source["ts"]
    if ts.dt.tz is None or str(ts.dt.tz) != "UTC" or (ts > BOUNDARY).any():
        raise ValueError(f"{kind} contains non-UTC or protected observations")
    source = source.loc[ts < BOUNDARY].copy()
    if source.empty:
        raise ValueError(f"{kind} is empty before protected cutoff")
    if kind == "oi":
        crash_data.validate_oi_quantity(source)
    attempt.inputs.frame(f"{kind}:{SYMBOL}", source)
    return source


def _descriptive_result(outcome, frame: pd.DataFrame) -> dict:
    """Gross references and costs; NOT marked equity or statistical edge."""
    rows = outcome.trades
    counts = {reason: sum(t.exit_reason == reason for t in rows) for reason in ("stop", "target", "time")}
    # Trade-return totals are fixed-base, per-entry-reference fractions.
    # They are NOT portfolio returns; cross-day open inventory is unmarked.
    exit_dates = pd.date_range(frame.index[0].floor("D"), frame.index[-1].floor("D"), freq="D")
    by_day = {d.date().isoformat(): 0 for d in exit_dates}
    for trade in rows:
        day = pd.Timestamp(trade.exit_bar_end_ns, unit="ns", tz="UTC").date().isoformat()
        by_day[day] += 1
    result = {
        "model": MODEL,
        "status": "RETROSPECTIVE_UNQUALIFIED" if not outcome.halted else "UNAVAILABLE_OPEN_INVENTORY",
        "qualified": False,
        "holdout_used": False,
        "account_equity_modeled": False,
        "trades": len(rows),
        "signals": len(outcome.census.candidate_times_ns),
        "signal_vetoes": outcome.census.veto_counts,
        "valid_bars": outcome.census.valid_bars,
        "quarantined_bars": outcome.census.discarded_quality_bars,
        "skipped_before_entry": outcome.skipped_before_entry,
        "ignored_while_open": outcome.ignored_while_open,
        "unresolved_after_entry": outcome.unresolved_after_entry,
        "halted": outcome.halted,
        "exit_counts": counts,
        "trade_count_by_exit_day_utc": by_day,
        "limitation": (
            "Trade-count census and reference OHLC fills only; missing marked equity, "
            "authenticated funding, spread/impact evidence, real latency, liquidation and portfolio margin."
        ),
    }
    if not outcome.halted:
        result["sum_net_trade_return_not_portfolio"] = outcome.total_net_trade_return
        result["mean_net_trade_bps"] = (
            sum(t.net_return for t in rows) * 10_000 / len(rows) if rows else None
        )
    return result


def evaluate(config_path: Path) -> dict:
    cfg = _config(config_path)
    expected = member(config_path)
    _registered(cfg.results_dir, expected)
    with trial_run(
        cfg.results_dir, expected["kind"], expected["instrument"], expected["params"], tuple(expected["window"])
    ) as trial:
        root = cfg.data_dir / "crash"
        klines = _load_preholdout(root, "klines", attempt=trial)
        oi = _load_preholdout(root, "oi", attempt=trial)
        frame = crash_study.bars(klines, oi)
        if frame.empty:
            raise ValueError("no protected research bars available")
        # The ensemble doesn't use OI as an indicator, but it must not
        # accept a candle for which source OI quality/coverage is unknown.
        frame["oi_invalid"] = frame["oi_invalid"] | frame["oi"].isna()
        outcome = replay_research_frame(
            frame, boundary=BOUNDARY, costs=COSTS, signal_settings=SETTINGS, assumptions=ASSUMPTIONS
        )
        trial.inputs.frame("screened-bars:BTCUSDT", frame.reset_index(names="ts"))
        trial.window = (int(frame.index[0].value), int(frame.index[-1].value))
        trial.result = _descriptive_result(outcome, frame)
    return trial.result


def _write_exclusive(path: Path, payload: dict) -> None:
    # Do not overwrite frozen plan files.
    raw = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Registered research-only BTC bearish ensemble OHLC study")
    parser.add_argument("--config", type=Path, required=True)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--plan-out", type=Path, help="freeze proposed plan; read no market bars")
    command.add_argument("--evaluate", action="store_true", help="requires exact registered plan")
    args = parser.parse_args(argv)
    try:
        if args.plan_out is not None:
            _write_exclusive(args.plan_out, proposed_plan(args.config))
            print(json.dumps({"proposed_plan": str(args.plan_out), "model": MODEL}, sort_keys=True))
        else:
            print(json.dumps(evaluate(args.config), indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
