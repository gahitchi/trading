"""
Paper-only guarantees and the kill switch.

Three independent layers must all agree before an order can leave the process:

1. settings check before connecting: a paper API port + a paper account ID ("DU...")
2. the ExecutionAgent re-checks the account the broker actually reports before
   EVERY order, and shuts the system down if it is not that paper account
3. there is no code path that builds a live-money configuration

Kill switch: a small JSON file. While it exists, the ExecutionAgent sends no new
orders and cancels open ones within a minute ("halt"), or exits every position at
the next open ("flatten"). `lab kill` / `lab resume` manage it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

# 4004: paper API relayed by the ib-gateway container (ghcr.io/gnzsnz/ib-gateway)
PAPER_PORTS = {4002: "IB Gateway paper", 7497: "TWS paper", 4004: "IB Gateway paper (Docker relay)"}
LIVE_PORTS = {4001: "IB Gateway live", 7496: "TWS live", 4003: "IB Gateway live (Docker relay)"}
_PAPER_ACCOUNT = re.compile(r"DU\d{5,}")


class SafetyError(Exception):
    pass


def check_paper_settings(port: int, account_id: str) -> None:
    if port in LIVE_PORTS:
        raise SafetyError(f"port {port} is the {LIVE_PORTS[port]} port. This lab trades paper accounts only.")
    if port not in PAPER_PORTS:
        known = ", ".join(f"{p} = {n}" for p, n in PAPER_PORTS.items())
        raise SafetyError(f"port {port} is not a known paper port ({known}).")
    if not account_id:
        raise SafetyError(
            "set the IB paper account ID (it starts with 'DU'): [paper] account_id in lab.toml "
            "or LAB_PAPER_ACCOUNT in the environment."
        )
    if not _PAPER_ACCOUNT.fullmatch(account_id):
        raise SafetyError(
            f"account {account_id!r} is not an IB paper account ID (paper accounts look like 'DU1234567')."
        )


def is_paper_account(account_id: str) -> bool:
    """Accepts Nautilus account IDs such as 'IB-DU1234567'."""
    number = str(account_id).split("-", 1)[-1]
    return bool(_PAPER_ACCOUNT.fullmatch(number))


# ----------------------------------------------------------------------------- kill switch


def set_kill_switch(path: Path, mode: str, reason: str) -> dict:
    if mode not in ("halt", "flatten"):
        raise ValueError("mode must be 'halt' or 'flatten'")
    state = {"mode": mode, "reason": reason, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return state


def read_kill_switch(path: str | Path) -> dict | None:
    p = Path(path)
    try:
        if not p.exists():
            return None
        raw = p.read_text()
    except OSError as e:
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


def clear_kill_switch(path: Path) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False
