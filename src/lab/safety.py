"""
Paper-only guarantees. Three independent layers must all agree before an order
can leave the process in paper mode:

1. settings check before connecting: paper port + a paper account ID ("DU...")
2. the ExecutionAgent re-checks the account the broker actually reports before
   EVERY order, and shuts the system down if it is not a paper account
3. there is no code path that builds a live-money configuration
"""

from __future__ import annotations

import re

PAPER_PORTS = {4002: "IB Gateway paper", 7497: "TWS paper"}
LIVE_PORTS = {4001: "IB Gateway live", 7496: "TWS live"}
_PAPER_ACCOUNT = re.compile(r"DU\d{5,}")


class SafetyError(Exception):
    pass


def check_paper_settings(port: int, account_id: str) -> None:
    if port in LIVE_PORTS:
        raise SafetyError(f"port {port} is the {LIVE_PORTS[port]} port. This lab trades paper accounts only.")
    if port not in PAPER_PORTS:
        raise SafetyError(f"port {port} is not a known paper port ({', '.join(f'{p} = {n}' for p, n in PAPER_PORTS.items())}).")
    if not account_id:
        raise SafetyError("set paper.account_id in lab.toml to your IB paper account ID (it starts with 'DU').")
    if not _PAPER_ACCOUNT.fullmatch(account_id):
        raise SafetyError(f"account {account_id!r} is not an IB paper account ID (paper accounts look like 'DU1234567').")


def is_paper_account(account_id: str) -> bool:
    """Accepts Nautilus account IDs such as 'IB-DU1234567'."""
    number = str(account_id).split("-", 1)[-1]
    return bool(_PAPER_ACCOUNT.fullmatch(number))
