"""
Hard halts: stops that only an operator may clear.

The system runs unattended by default (Gabriel's decision, collab/decisions.md), so it
must tell apart two kinds of trouble:

  transient  disconnects, gateway or container restarts, late data, a rejected order.
             The node resumes by itself once every start-up check passes again (paper
             account, reconciliation of positions/cash/orders, fresh bars). Nothing to clear.
  hard       the drawdown circuit breaker, a broker that is not the configured paper
             account, a ledger/broker difference that fill recovery cannot explain, margin
             below maintenance. Trading stays stopped, across restarts, until an operator
             runs `lab recover --reason "..."`, which is recorded in recoveries.jsonl.

The halt is a JSON file next to the ledger (results/paper/HALT). The first reason is kept;
later hard events while halted are appended to its history. The ExecutionAgent re-reads
the file before every rebalance, so `lab recover` takes effect without a restart.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

HARD_KINDS = {
    "circuit_breaker": "equity fell below the drawdown limit from its peak",
    "paper_guard": "the broker reported an account other than the configured paper account",
    "reconciliation_mismatch": "the ledger and the broker disagree and fill recovery could not explain it",
    "margin_call": "equity fell below the maintenance requirement (or to zero): positions may only shrink",
    "cash_mismatch": "the broker account's cash cannot support the lab's ledger",
    "invalid_control_file": "a control file could not be read as a halt record; trading stays stopped",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_halt(path: str | Path) -> dict | None:
    """The active hard halt, or None. Anything present but not a well-formed halt record (bad JSON, a list,
    a number, an unknown kind, unreadable bytes) is itself a hard halt: fail closed, never crash (G007-F11)."""
    p = Path(path)
    try:
        if not p.exists():
            return None
        raw = p.read_text()
    except (OSError, UnicodeError) as e:
        return {"kind": "invalid_control_file", "reason": f"cannot read {p}: {e}", "at": _now(), "history": []}
    try:
        state = json.loads(raw or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        state = None
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("kind"), str)
        or state["kind"] not in HARD_KINDS
        or not isinstance(state.get("history", []), list)
        or any(
            not isinstance(h, dict)
            or not isinstance(h.get("kind"), str)
            or h["kind"] not in HARD_KINDS
            or not isinstance(h.get("reason", ""), str)
            for h in state.get("history", [])
        )
        or not isinstance(state.get("reason", ""), str)
    ):
        return {
            "kind": "invalid_control_file",
            "reason": f"{p} is not a valid halt record: {raw[:200]!r}",
            "at": _now(),
            "history": [],
        }
    state.setdefault("history", [])
    if not isinstance(state["history"], list):
        state["history"] = []
    return state


def halt_kinds(state: dict | None) -> set[str]:
    """Every active reason matters, including those appended after the first halt."""
    if state is None:
        return set()
    return {state["kind"], *(h["kind"] for h in state.get("history", []))}


def set_halt(path: str | Path, kind: str, reason: str) -> dict:
    if kind not in HARD_KINDS:
        raise ValueError(f"{kind!r} is not a hard halt kind ({sorted(HARD_KINDS)})")
    p = Path(path)
    current = read_halt(p)
    if current is not None:
        current.setdefault("history", []).append({"kind": kind, "reason": reason, "at": _now()})
        state = current
    else:
        state = {"kind": kind, "reason": reason, "at": _now(), "history": []}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(p)
    return state


def clear_halt(path: str | Path, operator_reason: str, *, audit: str | Path) -> dict | None:
    """Clear a hard halt. Requires a reason, which is appended to the audit log with the halt."""
    if not operator_reason or not operator_reason.strip():
        raise ValueError("a reason is required to clear a hard halt")
    p = Path(path)
    state = read_halt(p)
    if state is None:
        return None
    record = {"cleared_at": _now(), "operator_reason": operator_reason.strip(), "halt": state}
    a = Path(audit)
    a.parent.mkdir(parents=True, exist_ok=True)
    with a.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    p.unlink()
    return record
