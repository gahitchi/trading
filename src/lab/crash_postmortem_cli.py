"""One pre-registered diagnostic replay of the existing BTC bearish breakdown.

This is a *postmortem*, not a new candidate, grid search or a holdout
evaluation. Its 16 calendar placebo seeds are fixed in source code;
all are reported, none are selected for performance.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from lab import crash_data, crash_funding, crash_funding_ledger
from lab import crash_placebo, crash_postmortem, crash_qualification
from lab import crash_study as study
from lab.config import load_config
from lab.persistence import atomic_json, nullable_numbers, read_json
from lab.trials import trial_run

VERSION = "btc-breakdown-postmortem-and-month-hour-exposure-placebo-v1"


def execute(config_path: Path, funding_plan_path: Path, *, report_out: Path | None = None) -> dict:
    """Reserve a durable diagnostic attempt before any actual market-data read."""
    config_path, funding_plan_path = Path(config_path), Path(funding_plan_path)
    if report_out is not None:
        report_out = Path(report_out)
        if report_out.exists() or report_out.is_symlink():
            raise ValueError("refusing to overwrite existing diagnostic report")
    cfg = load_config(config_path)
    root = cfg.data_dir / "crash"
    boundary = study.boundary(cfg.results_dir, root)
    if boundary != pd.Timestamp("2026-05-08T00:00:00Z"):
        # Exact UTC midnight, including timezone, prevents a shifted
        # or malformed partition from silently expanding research.
        raise ValueError("diagnostic requires accepted BTC research boundary 2026-05-08T00:00Z")
    settings = study.CrashSettings.preset("breakdown")
    plan = read_json(funding_plan_path)
    if not isinstance(plan, dict):
        raise ValueError("invalid funding plan metadata")
    plan_sha = crash_funding._digest(plan)
    params = {
        **study.params_of(settings, ["BTCUSDT"]),
        "analysis_policy": VERSION,
        "execution_policy": "reference-taker-fixed-base-two-fill-notional-fees-v1",
        "funding_policy": crash_funding_ledger.MODEL,
        "funding_source_plan_sha256": plan_sha,
        "funding_events_sha256": plan.get("events_sha256"),
        "placebo_policy": crash_placebo.POLICY,
        "placebo_seeds": list(crash_placebo.SEEDS),
        "not_a_strategy_parameter_search": True,
    }
    with trial_run(
        cfg.results_dir, "crash-study", "crash-breakdown", params,
        (None, int(boundary.value) - 1),
    ) as attempt:
        funding = crash_funding.load_verified(config_path, funding_plan_path, symbol="BTCUSDT")
        if funding["plan_sha256"] != plan_sha:
            raise ValueError("funding source plan changed after research reservation")
        source = funding["events"]
        attempt.inputs.frame("funding:BTCUSDT", pd.DataFrame({
            "ts": pd.to_datetime([e["fundingTime"] for e in source], unit="ms", utc=True),
            "fundingRate": [e["fundingRate"] for e in source],
            "markPrice": [e["markPrice"] for e in source],
        }))
        run = study.run(
            root, ["BTCUSDT"], settings, boundary, holdout=False, evidence=attempt.inputs,
        )
        if "BTCUSDT" not in run.trades or run.trades["BTCUSDT"].empty:
            raise ValueError("no historic BTCUSDT breakdown trades to diagnose")
        funded = crash_funding_ledger.funded_run(run, -1, funding)
        original = funded.trades["BTCUSDT"]
        klines = crash_data.load(root, "klines", "BTCUSDT", before=boundary)
        if klines is None:
            raise ValueError("cannot reopen pre-holdout candle inputs for diagnostics")
        klines = klines.loc[klines["ts"] < boundary].copy()
        attempt.inputs.frame("diagnostic:klines:BTCUSDT", klines)
        candles = klines.set_index("ts")
        detail = crash_postmortem.attribution(original, candles, -1)
        failures = crash_postmortem.describe(detail)
        outcomes = []
        for seed in crash_placebo.SEEDS:
            raw = crash_placebo.matched_placebo(
                candles, original, side=-1, fee_bps=settings.fee_bps,
                slippage_bps=settings.slippage_bps, seed=seed,
            )
            valued = crash_funding_ledger.funded_trades(raw, -1, funding)
            matched = crash_placebo.comparison(original, valued)
            outcomes.append({"seed": seed, **matched})
        differences = np.array([x["paired_mean_difference_bps"] for x in outcomes])
        placebos = {
            "policy": crash_placebo.POLICY,
            "n_predeclared_seeds": len(outcomes),
            "scenarios": outcomes,
            "median_paired_difference_bps": float(np.median(differences)),
            "minimum_paired_difference_bps": float(differences.min()),
            "maximum_paired_difference_bps": float(differences.max()),
            "not_a_p_value_or_confidence_interval": True,
        }
        qualification = crash_qualification.inventory(cfg.results_dir)
        report = {
            "kind": VERSION,
            "scope": "pre_holdout_postmortem_only",
            "funding_source_plan_sha256": plan_sha,
            "original_trade_count": len(original),
            "original_funded_mean_net_bps": float(original["net"].mean() * 1e4),
            "failure_analysis": failures,
            "matched_placebos": placebos,
            "qualification": qualification,
            "per_trade_diagnostics": [
                {
                    k: (v.isoformat() if isinstance(v, pd.Timestamp) else v)
                    for k, v in row.items()
                }
                for row in detail.to_dict("records")
            ],
        }
        report = nullable_numbers(report)
        # Bind every per-trade diagnostic and all sixteen placebo
        # scenarios to the durable attempt, not only their aggregate.
        report["analysis_payload_sha256"] = crash_funding._digest({
            "failure_analysis": report["failure_analysis"],
            "matched_placebos": report["matched_placebos"],
            "per_trade_diagnostics": report["per_trade_diagnostics"],
        })
        attempt.result = {
            "analysis": VERSION,
            "trades": len(original),
            "original_mean_net_bps": report["original_funded_mean_net_bps"],
            "placebo_median_difference_bps": placebos["median_paired_difference_bps"],
            "qualified": False,
            "funding_source_plan_sha256": plan_sha,
            "analysis_payload_sha256": report["analysis_payload_sha256"],
        }
        attempt.window = (int(run.start.value), int(run.end.value))
    # Recount the completed diagnostic without excluding historical failures.
    report["qualification"] = crash_qualification.inventory(cfg.results_dir)
    if report_out is not None:
        atomic_json(report_out, report)
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="BTC bearish crash failure analysis, size/duration matched placebo")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--funding-plan", required=True, type=Path)
    p.add_argument("--report-out", type=Path)
    a = p.parse_args(argv)
    try:
        result = execute(a.config, a.funding_plan, report_out=a.report_out)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        p.error(str(exc))
    print(json.dumps({
        "kind": result["kind"],
        "scope": result["scope"],
        "original_trades": result["original_trade_count"],
        "failure_analysis": result["failure_analysis"],
        "placebo_ensemble": {
            k: v for k, v in result["matched_placebos"].items()
            if k != "scenarios"
        },
        "qualification": result["qualification"],
        "report_out": str(a.report_out) if a.report_out else None,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
