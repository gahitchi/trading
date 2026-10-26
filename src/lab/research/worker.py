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
from dataclasses import replace
from pathlib import Path

from lab import data, experiments
from lab.config import load_config
from lab.research.approval import deployment_context, sha256_file
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
    # The swarm researches daily bars only (intraday history is too short for its train/test/holdout split).
    # A quick [trading] speed is for the node; research keeps working, validated and bound at "daily".
    cfg = replace(cfg, trading=replace(cfg.trading, speed="daily"))
    digest = sha256_file(hdir / "signal.py")
    spec = SignalSpec(hdir.name, {}, str(hdir / "signal.py"), digest)
    frames = data.load_many(cfg, tickers)
    from lab.provenance import frame_digest

    # Custom has a legacy relaxed *exploratory* screen, but promotion must
    # validate exactly the actual configured paper risk policy. Never hash
    # scientific screening limits as though the broker would enforce them.
    bound_risk = cfg.risk.profile == "custom"
    bounds = {"bound_risk": True} if bound_risk else {}
    validation = {
        "dataset_sha256": frame_digest(frames),
        "source_sha256": digest,
        "params": {},
        "config_fingerprint": experiments.config_fingerprint(cfg, len(tickers), **bounds),
        "deployment": deployment_context(cfg.snapshot(), tickers=tickers),
    }
    r = cfg.research

    if mode == "train":
        period = cfg.period("train")
        comp, _ = experiments.compare(cfg, [spec], tickers, period, frames, **bounds)
        cand = comp.rows[1]
        edge = cand["sharpe_edge"]
        null = experiments.null_test(
            cfg, spec, tickers, period, r.null_samples, frames, observed_edge=edge, **bounds
        )
        checks = {
            "sharpe_edge >= min_sharpe_edge": edge >= r.min_sharpe_edge,
            "p_value <= max_p_value": null["p_value"] <= r.max_p_value,
            "deflated Sharpe >= min_dsr": cand["dsr"] >= r.min_dsr,
            "no signal errors": cand["signal_errors"] == 0,
            "traded at all": cand.get("fills", 0) > 0,
        }
        from lab.factor_stats import diagnose

        out = {
            "validation": validation,
            "period": "train",
            "tickers": tickers,
            "rows": comp.rows,
            "null": null,
            # Stage-0 diagnostics (F-R15): predictive power, cost drag and turnover apart. Informational only.
            "diagnostics": diagnose(cfg, spec, tickers, frames, period, cand.get("active_sharpe", float("nan"))),
            "criteria": {"min_sharpe_edge": r.min_sharpe_edge, "max_p_value": r.max_p_value, "min_dsr": r.min_dsr},
            "checks": checks,
            "passed": all(checks.values()),
        }
        (hdir / "train.json").write_text(json.dumps(out, indent=2, default=str))
    elif mode == "test":
        train = json.loads((hdir / "train.json").read_text())
        if train.get("passed") is not True or train.get("validation") != validation:
            raise ValueError("test requires passing train evidence for the same source and configuration")
        period = cfg.period("test")
        comp, _ = experiments.compare(cfg, [spec], tickers, period, frames, **bounds)
        edge = comp.rows[1]["sharpe_edge"]
        out = {
            "validation": validation,
            "period": "test",
            "tickers": tickers,
            "rows": comp.rows,
            "sharpe_edge": edge,
            "survived": edge > 0 and comp.rows[1]["signal_errors"] == 0,
        }
        (hdir / "test.json").write_text(json.dumps(out, indent=2, default=str))
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
