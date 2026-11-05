"""Guarded, single-candidate BTC failed-retest research with immutable family gate.

--plan-out writes a frozen proposed research member without reading
historical bars or creating a holdout. The operator must explicitly
register that plan using 'lab research register' before --evaluate.
A study never claims qualified inference, forward-holdout evidence,
or production execution realism.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pandas as pd

from lab import bearish_retest as candidate
from lab import crash_data, crash_execution, crash_funding, crash_funding_ledger, crash_study
from lab.config import load_config
from lab.persistence import nullable_numbers, read_json
from lab.research_family import _digest, load_family
from lab.trials import _code_identity, code_provenance, trial_run

KEY = "btc-failed-retest-v1"
PLAN_NAME = "BTCUSDT frozen failed-retest v1; corrected end-stamped archive replay v2"
POLICY = "guarded-preregistered-funding-adjusted-btc-retest-study-v2-archive-endstamp"
BOUNDARY = pd.Timestamp("2026-05-08T00:00:00Z")


def _sources(config_path: Path, funding_plan_path: Path) -> tuple:
    """Validate metadata only. No candle/OI files or holdout records opened."""
    cfg = load_config(config_path)
    partition_path = cfg.results_dir / "crash_partition.json"
    partition = read_json(partition_path)
    if (
        not isinstance(partition, dict)
        or partition.get("version") != 1
        or pd.Timestamp(partition.get("holdout_start")) != BOUNDARY
    ):
        raise ValueError("accepted BTCUSDT holdout boundary is missing or changed")
    plan = read_json(funding_plan_path)
    crash_funding._validate_plan(plan, "BTCUSDT", BOUNDARY.date())
    if plan["start"] != "2024-01-01":
        raise ValueError("candidate requires the accepted 2024-01-01 funding start")
    return cfg, plan


def _params(plan: dict) -> dict:
    """Exact strategy constants are frozen by both code and the plan."""
    return {
        "candidate": candidate.MODEL,
        "study_policy": POLICY,
        "symbol": "BTCUSDT",
        "side": -1,
        "support_minutes": candidate.SUPPORT_MINUTES,
        "breach_fraction": candidate.BREACH_FRACTION,
        "volume_minutes": candidate.VOLUME_MINUTES,
        "volume_multiple": candidate.VOLUME_MULTIPLE,
        "reference_hours": candidate.PRIOR_HOURS,
        "retest_distance": candidate.RETEST_DISTANCE,
        "retest_minutes": candidate.RETEST_MINUTES,
        "confirm_minutes": candidate.CONFIRM_MINUTES,
        "confirm_drop": candidate.CONFIRM_DROP,
        "stop_buffer": candidate.STOP_BUFFER,
        "reward_to_risk": candidate.REWARD_TO_RISK,
        "max_hold_minutes": candidate.MAX_HOLD_MINUTES,
        "fee_bps": candidate.FEE_BPS,
        "slippage_bps": candidate.SLIPPAGE_BPS,
        "vol_target": candidate.VOL_TARGET,
        "max_weight": candidate.MAX_WEIGHT,
        "execution_model": crash_execution.MODEL,
        "funding_model": crash_funding_ledger.MODEL,
        "funding_plan_sha256": crash_funding._digest(plan),
        "funding_events_sha256": plan["events_sha256"],
        "data_policy": "preholdout-archive-minutebar-endstamp-cutoff-and-source-veto-v2",
        "holdout_start": BOUNDARY.isoformat(),
        "research_status": "retrospective-development-only",
    }


def member(config_path: Path, funding_plan_path: Path) -> dict:
    _cfg, plan = _sources(config_path, funding_plan_path)
    return {
        "kind": "crash-study", "instrument": KEY,
        "params": _params(plan),
        "window": [None, int(BOUNDARY.value) - 1],
    }


def proposed_plan(config_path: Path, funding_plan_path: Path) -> dict:
    return {
        "name": PLAN_NAME,
        "rationale": (
            "Frozen single failed-retest candidate; retains all earlier failed "
            "BTC crash trials. Retrospective development on inspected data "
            "cannot qualify statistical inference or the protected holdout."
        ),
        "members": [member(config_path, funding_plan_path)],
    }


def _registered(cfg, expected: dict) -> None:
    """Require the exact member and the original registered code/runtime."""
    family = load_family(cfg.results_dir)
    if family is None:
        raise ValueError("no research family registered; plan and register before evaluation")
    approved = {_digest(item) for item in family["plan"]["members"]}
    if _digest(expected) not in approved:
        raise ValueError("frozen candidate is not an exact member of the registered research plan")
    if _code_identity(family["code"]) != _code_identity(code_provenance()):
        raise ValueError("registered research code/runtime differs; cannot claim frozen evaluation")


def evaluate(config_path: Path, funding_plan_path: Path) -> dict:
    """Register a durable attempt first; then read verified pre-holdout evidence."""
    cfg, plan = _sources(config_path, funding_plan_path)
    expected = member(config_path, funding_plan_path)
    _registered(cfg, expected)
    params = expected["params"]
    with trial_run(
        cfg.results_dir, expected["kind"], expected["instrument"], params,
        tuple(expected["window"]),
    ) as attempt:
        funding = crash_funding.load_verified(config_path, funding_plan_path, symbol="BTCUSDT")
        if funding["plan_sha256"] != params["funding_plan_sha256"]:
            raise ValueError("funding evidence plan changed after reservation")
        events = funding["events"]
        attempt.inputs.frame("funding:BTCUSDT", pd.DataFrame({
            "ts": pd.to_datetime([row["fundingTime"] for row in events], unit="ms", utc=True),
            "fundingRate": [row["fundingRate"] for row in events],
            "markPrice": [row["markPrice"] for row in events],
        }))

        root = cfg.data_dir / "crash"
        # The data loader filters out boundary-overlapping archives
        # BEFORE opening any parquet file.
        k = crash_data.load(root, "klines", "BTCUSDT", before=BOUNDARY)
        if k is None or k.empty:
            raise ValueError("missing pre-holdout candle data")
        # Binance daily archives use a minute-END timestamp: the
        # 2026-05-07 daily file may legitimately end at 2026-05-08
        # 00:00 UTC (the 23:59-00:00 bar). Its data is still
        # from before the protected holdout, but the old crash
        # study excludes boundary-labelled observations. Mirror
        # that rule rather than rejecting the entire source file.
        # Files containing a timestamp strictly AFTER the cutoff
        # are malformed and fail closed, never silently truncated.
        if (k["ts"] > BOUNDARY).any():
            raise ValueError("pre-holdout candle archive contains timestamp beyond protected boundary")
        k = k.loc[k["ts"] < BOUNDARY].copy()
        if k.empty:
            raise ValueError("no eligible pre-holdout candle data after UTC end-stamp cutoff")
        attempt.inputs.frame("klines:BTCUSDT", k)
        oi = crash_data.load(root, "oi", "BTCUSDT", before=BOUNDARY)
        if oi is None or oi.empty or "oi_qty" not in oi or "oi_valid" not in oi:
            raise ValueError("accepted OI contract quantity/quality source is missing")
        if (oi["ts"] > BOUNDARY).any():
            raise ValueError("pre-holdout OI archive contains timestamp beyond protected boundary")
        oi = oi.loc[oi["ts"] < BOUNDARY].copy()
        if oi.empty:
            raise ValueError("no eligible pre-holdout OI source after UTC cutoff")
        attempt.inputs.frame("oi:BTCUSDT", oi)
        grid = crash_study.bars(k, oi)
        outcome = candidate.simulate(grid, boundary=BOUNDARY)
        funded = crash_funding_ledger.funded_trades(outcome.trades, -1, funding)
        summary = crash_study.summary(
            {"BTCUSDT": funded}, grid.index[0], grid.index[-1],
        )
        stress = crash_execution.stress_summary(
            {"BTCUSDT": funded}, -1, grid.index[0], grid.index[-1],
            crash_study.summary,
        )
        totals = crash_funding_ledger.funding_totals({"BTCUSDT": funded})
        report = nullable_numbers({
            "candidate": candidate.MODEL,
            "status": "RETROSPECTIVE_UNQUALIFIED",
            "qualified": False,
            "holdout_used": False,
            "trade_count": len(funded),
            "events": outcome.event_counts,
            "summary": summary,
            "funding": totals,
            "cost_sensitivity": stress,
            "funding_source_sha256": plan["events_sha256"],
        })
        attempt.result = report
        attempt.window = (int(grid.index[0].value), int(grid.index[-1].value))
    return report


def _write_exclusive(path: Path, payload: dict) -> None:
    """A proposed plan is never silently replaced."""
    raw = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Frozen BTC failed-retest preholdout research")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--funding-plan", required=True, type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--plan-out", type=Path, help="propose exact immutable family plan; do not evaluate")
    choice.add_argument("--evaluate", action="store_true", help="requires already-registered exact plan")
    args = parser.parse_args(argv)
    try:
        if args.plan_out is not None:
            out = proposed_plan(args.config, args.funding_plan)
            _write_exclusive(args.plan_out, out)
            print(json.dumps({"proposed_plan": str(args.plan_out), "candidate": KEY}, sort_keys=True))
        else:
            print(json.dumps(evaluate(args.config, args.funding_plan), indent=2, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
