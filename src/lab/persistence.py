"""Durable, strict JSON checkpoints for safety-critical state."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


class StateError(RuntimeError):
    """A checkpoint is missing required fields, damaged or otherwise unsafe."""


def atomic_json_write(path: str | Path, payload: dict) -> None:
    """Replace a checkpoint durably. Never expose a partially written file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, allow_nan=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, p)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_risk_checkpoint(path: str | Path) -> dict:
    """Missing means never initialized; corrupt means MUST NOT be reset automatically."""
    p = Path(path)
    if not p.exists():
        return {"version": 1, "peak": 0.0, "halted": False, "reason": ""}
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        peak = state["peak"]
        halted = state["halted"]
        if (
            state.get("version") != 1
            or isinstance(peak, bool)
            or not isinstance(peak, (int, float))
            or not math.isfinite(peak)
            or peak < 0
            or type(halted) is not bool
        ):
            raise ValueError("invalid risk checkpoint fields")
        return {"version": 1, "peak": float(peak), "halted": halted, "reason": str(state.get("reason", ""))}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise StateError(f"risk checkpoint {p} cannot be trusted: {exc}") from exc


def risk_checkpoint(peak: float, halted: bool, reason: str) -> dict:
    if not math.isfinite(peak) or peak < 0:
        raise StateError(f"invalid high-water mark: {peak}")
    return {
        "version": 1,
        "peak": peak,
        "halted": halted,
        "reason": reason,
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
    }
