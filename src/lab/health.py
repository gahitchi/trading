"""Fail-closed status evaluation. A heartbeat proves only process liveness."""

from __future__ import annotations

import math


def assess_heartbeat(hb: dict | None, age_seconds: float | None, max_age: float, *, risk_halted: bool) -> list[str]:
    """Return operator-actionable failures, not a misleading liveness-only pass."""
    issues = []
    if not isinstance(hb, dict):
        return ["heartbeat missing or unreadable"]
    if age_seconds is None or not math.isfinite(age_seconds) or age_seconds < -10 or age_seconds > max_age:
        issues.append("heartbeat stale or clock skew")
    if hb.get("paper_account_ok") is not True:
        issues.append("verified paper account not available")
    if hb.get("kill_switch"):
        issues.append("kill switch active")
    if hb.get("safety_halt") is True or hb.get("halted") is True or risk_halted:
        issues.append("persistent safety/risk halt")
    diffs = hb.get("reconciliation_issues")
    if not isinstance(diffs, list):
        issues.append("broker reconciliation status unknown")
    elif any(
        not isinstance(d, str) or not d.startswith(("outstanding broker order ", "inflight broker order "))
        for d in diffs
    ):
        issues.append("unresolved broker position or cache mismatch")
    if type(hb.get("unresolved_orders")) is not int:
        issues.append("order-intent status unknown")
    return issues
