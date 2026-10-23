"""Offline component diagnostics for unresolved G009 findings; no broker or credentials."""

import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from lab import data, experiments, metrics
from lab.agents.allocator import AllocatorAgent
from lab.config import load_config
from lab.report import _monthly_table


def main():
    cfg = load_config(Path(__file__).resolve().parents[2] / "lab.toml")
    # Same positional sequence, offset by one actual session. The production
    # covariance estimator receives no per-observation timestamps to align it.
    prices = 100 * np.exp(np.cumsum(np.tile([0.02, -0.02], 16)))
    a = SimpleNamespace(
        closes={"A": prices, "B": prices}, config=SimpleNamespace(periods_per_year=252, max_leverage=2, target_vol=0.10)
    )
    actual = AllocatorAgent._vol_scale(a, {"A": 0.5, "B": -0.5})
    common = np.array([prices[1:], prices[:-1]])
    covariance = np.cov(np.diff(np.log(common), axis=1))
    weights = np.array([0.5, -0.5])
    aligned_vol = math.sqrt(float(weights @ covariance @ weights) * 252)
    expected = min(2, 0.10 / aligned_vol)
    evidence = {
        "volatility_alignment": {
            "A_sessions": "1..32",
            "B_sessions": "2..33",
            "positional_scale": actual,
            "aligned_scale": expected,
            "aligned_annual_vol": aligned_vol,
        }
    }
    with tempfile.TemporaryDirectory(prefix="audit-partition-") as tmp:
        c = replace(
            cfg, results_dir=Path(tmp), trading=replace(cfg.trading, intraday_holdout=0.2, intraday_holdout_start="")
        )
        frames = {"GOOG": data.gbm(10, seed=7)}
        initial_train, initial_holdout = experiments.intraday_split(c, frames)
        experiments.guard_intraday(c, initial_train, frames)
        moved = replace(c, trading=replace(c.trading, intraday_holdout_start=str(frames["GOOG"].index[4].date())))
        _, new_holdout = experiments.intraday_split(moved, frames)
        evidence["partition_backdating"] = {
            "allowed_research": [str(initial_train.start), str(initial_train.end)],
            "original_holdout": str(initial_holdout.start),
            "new_holdout": str(new_holdout.start),
            "prior_training_now_labelled_holdout": new_holdout.start <= initial_train.end,
        }
    frame = data.gbm(3, seed=8)
    frame.iloc[0, frame.columns.get_loc("volume")] = np.nan
    cleaned = data.validate(frame, "GOOG")
    evidence["volume_repair"] = {
        "input": "NaN",
        "validated_volume": float(cleaned.iloc[0].volume),
        "event_converter": "universe.to_market_events and to_intraday_events force max(1, volume)",
    }
    curve = pd.Series([100.0, 0.0, 50.0], index=pd.bdate_range("2020-01-02", periods=3))
    evidence["insolvent_curve_metrics"] = metrics.summarize(curve)
    monthly = _monthly_table(pd.Series([100.0, 120.0], index=pd.to_datetime(["2020-01-02", "2020-01-31"])))
    evidence["first_month_reporting"] = {"actual_first_month_return": 0.2, "rendered_html": monthly}

    def safe(v):
        if isinstance(v, float) and not math.isfinite(v):
            return str(v)
        if isinstance(v, dict):
            return {k: safe(x) for k, x in v.items()}
        if isinstance(v, list):
            return [safe(x) for x in v]
        return v

    Path(sys.argv[1]).write_text(json.dumps(safe(evidence), indent=2) + "\n")


if __name__ == "__main__":
    main()
