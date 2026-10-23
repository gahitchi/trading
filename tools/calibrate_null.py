"""Bounded null-of-null experiment through actual engine/comparison/bootstrap paths.

No broker, LLM, network, or real holdout data. This is evidence for the stated
synthetic scenario, not a universal calibration or a profitability test.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from lab import data, experiments
from lab.config import Period, load_config
from lab.signals.registry import SignalSpec


def wilson(k: int, n: int) -> list[float]:
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0, c - h), min(1, c + h)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outer", type=int, default=40)
    ap.add_argument("--inner", type=int, default=39)
    ap.add_argument("--bars", type=int, default=504)
    ap.add_argument("--seed", type=int, default=73000)
    ap.add_argument("--config", type=Path, default=Path("lab.toml"))
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    if a.outer < 1 or a.inner < 19 or a.bars < 160:
        ap.error("need outer >= 1, inner >= 19, bars >= 160")
    cfg = load_config(a.config)
    candidates = [SignalSpec("sma_trend"), SignalSpec("rsi_reversion")]
    rows = []
    with tempfile.TemporaryDirectory(prefix="null-calibration-") as tmp:
        cfg = replace(
            cfg,
            root=Path(tmp),
            results_dir=Path(tmp) / "results",
            research_dir=Path(tmp) / "research",
            universe={"GOOG": "XNAS"},
            trading=replace(cfg.trading, speed="daily"),
        )
        for i in range(a.outer):
            frame = data.gbm(a.bars, seed=a.seed + i, start="2010-01-04", ann_drift=0, ann_vol=0.25)
            period = Period("synthetic-calibration", frame.index[120].date(), frame.index[-1].date())
            comp, _ = experiments.compare(cfg, candidates, ["GOOG"], period, {"GOOG": frame}, register=False)
            j = max(range(len(candidates)), key=lambda j: comp.rows[j + 1]["sharpe_edge"])
            row = comp.rows[j + 1]
            null = experiments.null_test(
                cfg,
                candidates[j],
                ["GOOG"],
                period,
                a.inner,
                {"GOOG": frame},
                workers=1,
                observed_edge=row["sharpe_edge"],
            )
            passed = (
                row["sharpe_edge"] >= cfg.research.min_sharpe_edge
                and null["p_value"] <= cfg.research.max_p_value
                and row["dsr"] >= cfg.research.min_dsr
                and row["signal_errors"] == 0
                and row.get("fills", 0) > 0
            )
            rows.append(
                {
                    "seed": a.seed + i,
                    "data_sha256": hashlib.sha256(frame.to_csv().encode()).hexdigest(),
                    "selected": candidates[j].name,
                    "edge": row["sharpe_edge"],
                    "dsr": row["dsr"],
                    "fills": row["fills"],
                    "p_value": null["p_value"],
                    "fallback_fraction": null["fallback_fraction_max"],
                    "training_gate_passed": passed,
                }
            )
            print(f"outer {i + 1}/{a.outer}: p={null['p_value']:.3f}, gate={passed}", file=sys.stderr, flush=True)
        rejected = sum(r["p_value"] <= 0.05 for r in rows)
        passed = sum(r["training_gate_passed"] for r in rows)
        report = {
            "scenario": "Zero arithmetic-drift GBM, 25% annual volatility, one stock; choose higher observed Sharpe edge of two fixed candidates, then evaluate its bootstrap p-value and complete training gate. 120 warm-up bars excluded from evaluation.",
            "limitations": [
                "Small Monte Carlo cohort; interval is wide",
                "No staggered histories, correlated panel, historical borrow, intraday data, model-generated selection or repeated human selection",
                "Bootstrap null is conditional on observed history; a single scenario cannot establish general validity",
                "This is the training gate, not test-period survival or live qualification",
            ],
            "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "source_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "--", "src"])).hexdigest(),
            "python": sys.version,
            "packages": {n: importlib.metadata.version(n) for n in ["nautilus_trader", "numpy", "pandas", "pyarrow"]},
            "config": cfg.snapshot(),
            "config_fingerprint": experiments.config_fingerprint(cfg),
            "outer": a.outer,
            "inner": a.inner,
            "bars": a.bars,
            "seed": a.seed,
            "nominal_alpha": 0.05,
            "null_rejections": rejected,
            "null_rejection_rate": rejected / a.outer,
            "null_rejection_wilson95": wilson(rejected, a.outer),
            "gate_passes": passed,
            "gate_pass_rate": passed / a.outer,
            "gate_pass_wilson95": wilson(passed, a.outer),
            "runs": rows,
        }
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
