"""Deployment self-check before the cloud move (and on your own machine): nothing exposed, nothing leaked.

    python3 tools/deployment_check.py            # from the repository root, on the machine that runs it

Checks:
  - every published Docker port is bound to 127.0.0.1 (the gateway API, VNC and the setup app)
  - the IBKR password file exists, is not empty, and only its owner can read it
  - .env and secrets/ exist locally but are not tracked by Git, and .gitignore covers them
  - the configured account is a paper account (DU...), on a paper port, with TRADING_MODE paper
  - on Linux with `ss`: nothing listens on the gateway/VNC/app ports on a public address

A deterministic checklist, not a penetration test: before leaving a server alone, also run an
independent scanner (for example Strix, RUNBOOK section 2) against it. Exit code 1 if anything fails.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PORTS = {4001, 4002, 4003, 4004, 5900, 7496, 7497, 8765}
LOCAL = ("127.0.0.1", "localhost", "[::1]", "::1")


def _published(compose: Path) -> list[str]:
    """Port entries under `ports:` keys, as written."""
    out, in_ports = [], False
    for raw in compose.read_text().splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if re.match(r"^\s*ports:\s*$", line):
            in_ports, indent = True, len(line) - len(line.lstrip())
            continue
        if in_ports:
            if line.strip().startswith("- "):
                out.append(line.strip()[2:].strip().strip("'\""))
            elif line.strip() and len(line) - len(line.lstrip()) <= indent:
                in_ports = False
    return out


def _env(path: Path) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def run(root: Path) -> list[tuple[str, str, str]]:
    results = []

    def add(status, name, detail):
        results.append((status, name, detail))

    for compose in sorted(root.glob("*compose*.yml")):
        for entry in _published(compose):
            host = entry.rsplit(":", 2)[0] if entry.count(":") >= 2 else ""
            ok = host in LOCAL
            add(
                "PASS" if ok else "FAIL",
                f"{compose.name} port {entry}",
                "bound to this machine only" if ok else "published on every interface: bind it to 127.0.0.1",
            )
        text = compose.read_text()
        if "TRADING_MODE" in text:
            ok = re.search(r"TRADING_MODE:\s*paper\b", text) is not None
            add("PASS" if ok else "FAIL", f"{compose.name} TRADING_MODE", "paper" if ok else "not paper")

    secret = root / "secrets" / "tws_password"
    if not secret.exists():
        add("FAIL", "secrets/tws_password", "missing (RUNBOOK section 3)")
    else:
        mode = stat.S_IMODE(secret.stat().st_mode)
        if secret.stat().st_size == 0:
            add("FAIL", "secrets/tws_password", "empty")
        elif os.name == "posix" and mode & 0o077:
            add("FAIL", "secrets/tws_password", f"mode {mode:o}: run chmod 600 secrets/tws_password")
        else:
            add("PASS", "secrets/tws_password", f"present, mode {mode:o}")

    git = shutil.which("git")
    if git and (root / ".git").exists():
        tracked = subprocess.run([git, "ls-files", ".env", "secrets"], cwd=root, capture_output=True, text=True).stdout
        add(
            "FAIL" if tracked.strip() else "PASS",
            "secrets not in Git",
            f"tracked: {tracked.split()}" if tracked.strip() else ".env and secrets/ are untracked",
        )
        for p in (".env", "secrets/tws_password"):
            ignored = subprocess.run([git, "check-ignore", "-q", p], cwd=root).returncode == 0
            add("PASS" if ignored else "FAIL", f".gitignore covers {p}", "yes" if ignored else "no: add it")

    env = _env(root / ".env")
    account = env.get("LAB_PAPER_ACCOUNT", "")
    add(
        "PASS" if account.startswith("DU") else "FAIL",
        "paper account",
        "a DU... paper account" if account.startswith("DU") else f"LAB_PAPER_ACCOUNT={account!r} is not a paper ID",
    )
    if env.get("LAB_IB_PORT") in ("4001", "7496", "4003"):
        add("FAIL", "IB port", f"LAB_IB_PORT={env['LAB_IB_PORT']} is a live-trading port")
    if env.get("LAB_TELEGRAM_TOKEN"):
        add("PASS", "alerts", "Telegram configured")
    else:
        add("WARN", "alerts", "no Telegram token: nobody hears about a halt while you are away (RUNBOOK section 7)")

    if sys.platform.startswith("linux") and shutil.which("ss"):
        listening = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout.splitlines()
        exposed = []
        for line in listening:
            parts = line.split()
            if len(parts) < 4:
                continue
            addr = parts[3]
            host, _, port = addr.rpartition(":")
            if port.isdigit() and int(port) in PORTS and host.strip("[]") not in ("127.0.0.1", "::1", "localhost"):
                exposed.append(addr)
        add(
            "FAIL" if exposed else "PASS",
            "listening sockets",
            f"public listeners on trading ports: {exposed}" if exposed else "no trading port listens publicly",
        )
    return results


def main() -> int:
    root = Path.cwd()
    results = run(root)
    for status, name, detail in results:
        print(f"{status:<4}  {name}: {detail}")
    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(failed)} failure(s). A clean checklist is not a penetration test; see RUNBOOK section 2.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
