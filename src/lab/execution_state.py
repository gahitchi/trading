"""Strict, deterministic checks for unattended paper execution state.

These routines do not depend on NautilusTrader and can be exercised under
fault-injection tests. They deliberately reject ambiguous state rather than
inventing order or cash history.
"""

from __future__ import annotations

import math


class ExecutionStateError(ValueError):
    """Execution state cannot be safely trusted or advanced."""


def _number(value, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExecutionStateError(f"{label} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ExecutionStateError(f"{label} must be finite and >= {minimum}")
    return value


def read_execution_state(raw: dict, known_tickers: set[str], *, live: bool = False) -> dict:
    """Validate a recovered checkpoint without silently discarding bad fields."""
    if not isinstance(raw, dict):
        raise ExecutionStateError("execution checkpoint must be a JSON object")
    if live and raw.get("version") != 2:
        raise ExecutionStateError("legacy checkpoint: verify broker state and migrate explicitly")
    cash = _number(raw.get("cash"), "cash", minimum=-1e12)
    commissions = _number(raw.get("commissions", 0), "commissions")
    positions = raw.get("positions", {})
    if not isinstance(positions, dict) or set(positions) - known_tickers:
        raise ExecutionStateError("checkpoint has malformed or unknown positions")
    positions = {t: _number(q, f"position {t}") for t, q in positions.items()}
    pending = raw.get("unresolved_orders", {})
    if not isinstance(pending, dict):
        raise ExecutionStateError("unresolved_orders must be a mapping")
    for oid, intent in pending.items():
        if not isinstance(oid, str) or not oid or not isinstance(intent, dict):
            raise ExecutionStateError("malformed order intent")
        if intent.get("ticker") not in known_tickers or intent.get("side") not in ("BUY", "SELL"):
            raise ExecutionStateError(f"invalid order identity: {oid}")
        total = _number(intent.get("quantity"), f"order quantity {oid}")
        filled = _number(intent.get("filled", 0), f"order filled {oid}")
        if total <= 0 or filled > total + 1e-8:
            raise ExecutionStateError(f"impossible order quantities for {oid}")
    usage = raw.get("daily_usage", {"session": "", "orders": 0, "bought": 0.0})
    if not isinstance(usage, dict) or not isinstance(usage.get("session"), str):
        raise ExecutionStateError("malformed daily limits")
    count = usage.get("orders")
    if type(count) is not int or count < 0:
        raise ExecutionStateError("malformed daily order count")
    _number(usage.get("bought"), "daily buy value")
    fills = raw.get("processed_fills", [])
    if not isinstance(fills, list) or any(not isinstance(x, str) or not x for x in fills):
        raise ExecutionStateError("malformed fill deduplication state")
    if len(fills) != len(set(fills)):
        raise ExecutionStateError("duplicated processed fill IDs")
    halted = raw.get("safety_halt", False)
    if type(halted) is not bool:
        raise ExecutionStateError("malformed safety halt")
    return {
        "cash": cash,
        "commissions": commissions,
        "positions": positions,
        "unresolved_orders": pending,
        "daily_usage": usage,
        "processed_fills": set(fills),
        "safety_halt": halted,
    }


def apply_order_fill(
    pending: dict, processed_fills: set[str], oid: str, trade_id: str, quantity: float
) -> bool:
    """Returns False for a replayed fill; unknown/overfilling events are fatal."""
    key = f"{oid}:{trade_id}"
    if key in processed_fills:
        return False
    if oid not in pending:
        raise ExecutionStateError(f"fill for unknown order intent {oid}")
    qty = _number(quantity, f"fill quantity {key}")
    if qty <= 0:
        raise ExecutionStateError(f"zero fill for {key}")
    intent = pending[oid]
    updated = float(intent["filled"]) + qty
    if updated > float(intent["quantity"]) + 1e-8:
        raise ExecutionStateError(f"fill exceeds requested quantity for {oid}")
    intent["filled"] = updated
    processed_fills.add(key)
    if updated >= float(intent["quantity"]) - 1e-8:
        del pending[oid]
    return True
