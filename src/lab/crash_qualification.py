"""Fail-closed crash research eligibility and full attempt-history inventory.

The existing independent daily-inference v1 calibration FAILED.
No Sharpe/t-statistic in this module can be used to graduate a
strategy or to enable a protected holdout.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from lab.trials import count_configurations, history

POLICY = "crash-research-no-inference-gate-v1"
BLOCKERS = (
    "Independent v1 daily-inference calibration failed; no validated replacement.",
    "Prospective entire-search-family qualification is not established.",
    "Observed fill timing, execution impact, and margin/liquidation are not validated.",
)


def inventory(results_dir: Path, *, key: str = "crash-breakdown") -> dict:
    """Retain completed, failed, reserved and legacy attempts in the count."""
    all_rows = history(results_dir)  # corrupt/inconsistent history raises
    rows = [
        r for r in all_rows
        if r["kind"] == "crash-study" and r["instrument"] == key and not r["holdout"]
    ]
    statuses = Counter(r.get("status", "legacy") for r in rows)
    incomplete = [
        r.get("attempt_id", "legacy") for r in rows
        if "code" not in r or "inputs" not in r
        or not r["inputs"].get("streams")
        or any(not stream.get("complete") for stream in r["inputs"]["streams"])
    ]
    return {
        "status": "NOT_QUALIFIED",
        "can_use_protected_holdout": False,
        "can_claim_statistical_significance": False,
        "policy": POLICY,
        "research_key": key,
        "all_historical_attempts": int(len(rows)),
        "distinct_configurations_or_code_versions": count_configurations(results_dir, "crash-study", key),
        "attempt_statuses": dict(sorted(statuses.items())),
        "attempts_with_missing_or_incomplete_provenance": len(incomplete),
        "blocked_on": list(BLOCKERS),
        "interpretation": (
            "These are research accounting and readiness diagnostics, not "
            "deflated Sharpe, an adjusted p-value or permission to test the holdout."
        ),
    }
