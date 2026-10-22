"""
Deterministic Validator, run in a separate process because it executes AI-written code.

    python -m lab.research.worker train <hypothesis_dir> <lab.toml>
    python -m lab.research.worker test  <hypothesis_dir> <lab.toml>

train: equal-weight portfolio of the universe vs buy-and-hold on the training period,
       then the bootstrap noise floor. Writes train.json with a pass/fail verdict.
test:  one run on the test period. Writes test.json. Only called after a train pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lab import data, experiments
from lab.config import load_config
from lab.signals.registry import SignalSpec


def available_tickers(cfg) -> list[str]:
    return experiments.research_tickers(cfg)[0]


def main(argv=None) -> int:
    mode, hdir, cfg_path = (argv or sys.argv[1:])[:3]
    hdir = Path(hdir)
    cfg = load_config(cfg_path)
    tickers = available_tickers(cfg)
    if not tickers:
        print("no data for any universe ticker; run: lab data fetch", file=sys.stderr)
        return 2
    spec = SignalSpec(hdir.name, {}, str(hdir / "signal.py"))
    frames = data.load_many(cfg, tickers)
    r = cfg.research

    if mode == "train":
        period = cfg.period("train")
        comp, _ = experiments.compare(cfg, [spec], tickers, period, frames)
        cand = comp.rows[1]
        edge = cand["sharpe_edge"]
        null = experiments.null_test(cfg, spec, tickers, period, r.null_samples, frames, observed_edge=edge)
        checks = {
            "sharpe_edge >= min_sharpe_edge": edge >= r.min_sharpe_edge,
            "p_value <= max_p_value": null["p_value"] <= r.max_p_value,
            "deflated Sharpe >= min_dsr": cand["dsr"] >= r.min_dsr,
            "no signal errors": cand["signal_errors"] == 0,
            "traded at all": cand.get("fills", 0) > 0,
        }
        out = {
            "period": "train",
            "tickers": tickers,
            "rows": comp.rows,
            "null": null,
            "criteria": {"min_sharpe_edge": r.min_sharpe_edge, "max_p_value": r.max_p_value, "min_dsr": r.min_dsr},
            "checks": checks,
            "passed": all(checks.values()),
        }
        (hdir / "train.json").write_text(json.dumps(out, indent=2, default=str))
    elif mode == "test":
        period = cfg.period("test")
        comp, _ = experiments.compare(cfg, [spec], tickers, period, frames)
        edge = comp.rows[1]["sharpe_edge"]
        out = {"period": "test", "tickers": tickers, "rows": comp.rows, "sharpe_edge": edge, "survived": edge > 0}
        (hdir / "test.json").write_text(json.dumps(out, indent=2, default=str))
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
