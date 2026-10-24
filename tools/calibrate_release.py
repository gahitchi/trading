"""Prospectively frozen synthetic engine calibration; resumable without retry erasure.

Each seed is a counted exposure, including refusals/failures. No real holdout,
market data, broker, or LLM is used. A failed criterion never adjusts production gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

import numpy as np

from lab import data, experiments
from lab.config import Period, load_config
from lab.persistence import atomic_json, read_json
from lab.provenance import source_digest
from lab.signals.registry import SignalSpec
from tools.calibrate_null import wilson

CASES = [
    {
        "name": "independent_fixed_252",
        "bars": 252,
        "stocks": 1,
        "correlation": 0.0,
        "stagger": 0,
        "selection": False,
        "power": False,
    },
    {
        "name": "correlated_selected_252",
        "bars": 252,
        "stocks": 2,
        "correlation": 0.65,
        "stagger": 0,
        "selection": True,
        "power": False,
    },
    {
        "name": "staggered_selected_504",
        "bars": 504,
        "stocks": 2,
        "correlation": 0.65,
        "stagger": 60,
        "selection": True,
        "power": False,
    },
    {
        "name": "predictable_selected_504",
        "bars": 504,
        "stocks": 2,
        "correlation": 0.65,
        "stagger": 0,
        "selection": True,
        "power": True,
    },
]


def trial(job):
    config, case, seed, inner = job
    with tempfile.TemporaryDirectory(prefix="release-calibration-") as tmp:
        cfg = load_config(config)
        tickers = ["GOOG", "TSLA"][: case["stocks"]]
        cfg = replace(
            cfg,
            root=Path(tmp),
            results_dir=Path(tmp) / "results",
            research_dir=Path(tmp) / "research",
            universe=dict.fromkeys(tickers, "XNAS"),
            trading=replace(cfg.trading, speed="daily"),
        )
        rng = np.random.default_rng(seed)
        count = 200 + case["bars"]
        common = rng.normal(size=count)
        frames = {}
        for i, ticker in enumerate(tickers):
            frame = data.gbm(count, seed=seed + i, start="2010-01-04", ann_drift=0, ann_vol=0.25)
            noise = np.sqrt(case["correlation"]) * common + np.sqrt(1 - case["correlation"]) * rng.normal(size=count)
            sigma = 0.003 if case["power"] else 0.25 / np.sqrt(252)
            predictable = 0.004 * np.where((np.arange(count) // 63) % 2 == 0, 1, -1) if case["power"] else 0
            logreturns = predictable - 0.5 * sigma * sigma + sigma * noise
            close = 100 * np.exp(np.cumsum(logreturns))
            opening = np.r_[100, close[:-1]]
            frame["open"], frame["close"] = opening, close
            frame["high"] = np.maximum(opening, close) * 1.001
            frame["low"] = np.minimum(opening, close) * 0.999
            frames[ticker] = frame.iloc[case["stagger"] if i else 0 :]
        period = Period(
            "synthetic-release-calibration", frames[tickers[0]].index[200].date(), frames[tickers[0]].index[-1].date()
        )
        specs = [SignalSpec("sma_trend", {"fast": 5, "slow": 20})]
        if case["selection"]:
            specs.append(SignalSpec("rsi_reversion"))
        result = {
            "case": case["name"],
            "seed": seed,
            "data_sha256": hashlib.sha256("".join(f.to_csv() for f in frames.values()).encode()).hexdigest(),
        }
        try:
            comp, _ = experiments.compare(cfg, specs, tickers, period, frames, register=False)
            selected = max(range(len(specs)), key=lambda j: comp.rows[j + 1]["sharpe_edge"])
            row = comp.rows[selected + 1]
            null = experiments.null_test(
                cfg, specs[selected], tickers, period, inner, frames, workers=1, observed_edge=row["sharpe_edge"]
            )
            result.update(
                selected=specs[selected].name,
                edge=row["sharpe_edge"],
                p_value=null["p_value"],
                dsr=row["dsr"],
                fallback=null["fallback_fraction_worst_stock"],
                rejected=null["p_value"] <= cfg.research.max_p_value,
                passed=row["sharpe_edge"] >= cfg.research.min_sharpe_edge
                and null["p_value"] <= cfg.research.max_p_value
                and row["dsr"] >= cfg.research.min_dsr
                and row["signal_errors"] == 0
                and row.get("fills", 0) > 0,
            )
        except Exception as exc:
            result.update(failed=True, error=f"{type(exc).__name__}: {exc}", passed=False, rejected=False)
        return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--config", type=Path, default=Path("lab.toml"))
    ap.add_argument("--outer", type=int, default=400)
    ap.add_argument("--inner", type=int, default=199)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument(
        "--power-pilot",
        action="store_true",
        help="bounded diagnostic only; run every planned power exposure without a release qualification claim",
    )
    a = ap.parse_args()
    if a.outer < 1 or a.inner < 19 or not 1 <= a.workers <= 16:
        ap.error("outer >= 1, inner >=19, workers 1..16 required")
    a.output.mkdir(parents=True, exist_ok=True)
    cases = [case for case in CASES if case["power"]] if a.power_pilot else CASES
    protocol = {
        "version": 1,
        "cases": cases,
        "power_pilot_only": a.power_pilot,
        "outer": a.outer,
        "inner": a.inner,
        "seed_start": 9280000,
        "config_sha256": hashlib.sha256(a.config.read_bytes()).hexdigest(),
        "source_sha256": source_digest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_gates": vars(load_config(a.config).research),
        "criteria": {
            "minimum_outer": 400,
            "minimum_inner": 199,
            "null_gate_wilson95_upper_max": 0.10,
            "power_gate_wilson95_lower_min": 0.80,
            "failed_exposures": 0,
            "stopping": "power first; stop when even every remaining planned exposure passing cannot reach the predeclared lower confidence bound",
        },
        "limitations": [
            "Synthetic daily long-only cases, not a profitability claim or deployment approval",
            "Selection is two fixed candidates, not adaptive LLM or repeated human search",
            "Failure/retry accounting is exercised separately by transactional holdout tests",
            "No historical borrow, intraday data, corporate actions or bank-policy promotion tested",
        ],
    }
    protocol_path = a.output / "protocol.json"
    if protocol_path.exists() and read_json(protocol_path) != protocol:
        raise SystemExit("protocol/source/config changed; refuse to combine or overwrite exposures")
    atomic_json(protocol_path, protocol)
    jobs = []
    for c, case in enumerate(cases):
        for i in range(a.outer):
            seed = protocol["seed_start"] + c * 100000 + i
            if not (a.output / f"{seed}.json").exists():
                jobs.append((str(a.config.resolve()), case, seed, a.inner))
    # A futile power study already fails release qualification. Do not spend the
    # remaining exposures pretending it could pass; do not reinterpret this as a
    # false-positive calibration. This rule is fixed in protocol.json before runs.
    jobs.sort(key=lambda j: not j[1]["power"])
    futile = False
    with ProcessPoolExecutor(a.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        for start in range(0, len(jobs), a.workers):
            batch = jobs[start : start + a.workers]
            # Reserve exposure BEFORE dispatch. A killed worker/run cannot erase
            # an attempted seed or silently retry it on resume.
            for job in batch:
                atomic_json(
                    a.output / f"{job[2]}.json",
                    {
                        "case": job[1]["name"],
                        "seed": job[2],
                        "failed": True,
                        "passed": False,
                        "rejected": False,
                        "error": "worker interrupted before durable result",
                    },
                )
            futures = {pool.submit(trial, job): job for job in batch}
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception as exc:
                    job = futures[future]
                    row = {
                        "case": job[1]["name"],
                        "seed": job[2],
                        "failed": True,
                        "passed": False,
                        "rejected": False,
                        "error": f"worker failure: {type(exc).__name__}",
                    }
                atomic_json(a.output / f"{row['seed']}.json", row)
            power_rows = [
                read_json(p) for p in a.output.glob("[0-9]*.json") if read_json(p)["case"] == "predictable_selected_504"
            ]
            failures = sum(not row["passed"] for row in power_rows)
            best_possible_lower = wilson(a.outer - failures, a.outer)[0]
            print(
                f"completed {min(start + a.workers, len(jobs))}/{len(jobs)}; power failures={failures}; best possible lower bound={best_possible_lower:.4f}",
                flush=True,
            )
            if best_possible_lower < 0.80 and not a.power_pilot:
                futile = True
                break
    rows = [read_json(p) for p in sorted(a.output.glob("[0-9]*.json"))]
    summaries = []
    for case in cases:
        group = [r for r in rows if r["case"] == case["name"]]
        if not group:
            summaries.append(
                {
                    "case": case["name"],
                    "exposures": 0,
                    "criterion_passed": False,
                    "reason": "not run after predeclared power futility",
                }
            )
            continue
        passed, rejected, failed = (sum(bool(r.get(key)) for r in group) for key in ("passed", "rejected", "failed"))
        interval = wilson(passed, len(group))
        qualified = (
            not a.power_pilot
            and len(group) >= 400
            and a.inner >= 199
            and failed == 0
            and (interval[0] >= 0.80 if case["power"] else interval[1] <= 0.10)
        )
        summaries.append(
            {
                "case": case["name"],
                "exposures": len(group),
                "failures": failed,
                "gate_passes": passed,
                "gate_pass_wilson95": interval,
                "null_rejections": rejected,
                "null_rejection_wilson95": wilson(rejected, len(group)),
                "criterion_passed": qualified,
            }
        )
    report = {
        "protocol": protocol,
        "stopped_for_power_futility": futile,
        "planned_exposures_per_case": a.outer,
        "protocol_sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
        "source_unchanged": source_digest() == protocol["source_sha256"],
        "cases": summaries,
        "runs": rows,
    }
    atomic_json(a.output / "summary.json", report)
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
