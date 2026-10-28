"""
The kill switch: a small JSON file. While it exists, strategies send no new orders and cancel open ones
("halt"), or also exit every position ("flatten"). `lab kill` / `lab resume` manage it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def set_kill_switch(path: Path, mode: str, reason: str, *, preserve_existing=False) -> dict:
    if mode not in ("halt", "flatten"):
        raise ValueError("mode must be 'halt' or 'flatten'")
    state = {"mode": mode, "reason": reason, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    from lab.persistence import atomic_json, file_lock

    with file_lock(path.with_name(path.name + ".lock")):
        previous = read_kill_switch(path)
        if preserve_existing and previous and (previous.get("reason") != reason or previous.get("mode") == "flatten"):
            return previous
        atomic_json(path, state)
    return state


def read_kill_switch(path: str | Path) -> dict | None:
    p = Path(path)
    try:
        if not p.exists():
            return None
        raw = p.read_text()
    except (OSError, UnicodeError) as e:
        return {"mode": "halt", "reason": f"cannot read kill file: {e}"}
    try:
        state = json.loads(raw or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        state = None
    if not isinstance(state, dict):
        # Any kill file stops trading, whatever it contains (G007-F11: a JSON list used to crash this reader).
        return {"mode": "halt", "reason": f"unreadable kill file: {raw[:200]!r}"}
    if state.get("mode") not in ("halt", "flatten"):
        state["mode"] = "halt"
    return state


def clear_kill_switch(path: Path, *, expected=None) -> bool:
    from lab.persistence import durable_unlink, file_lock

    with file_lock(path.with_name(path.name + ".lock")):
        if expected is not None and read_kill_switch(path) != expected:
            raise ValueError("stop state changed; refusing to clear it")
        if path.exists():
            durable_unlink(path)
            return True
        return False
