"""One-time local Docker setup, then open the app. Never submit an order."""

import getpass
import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path


def private_file(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())


def main():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["docker", "compose", "version"], check=True, cwd=root)
    if not (root / ".env").exists():
        print("Connect IB Gateway in PAPER mode. Credentials are saved only on this computer.")
        username = input("IBKR username: ").strip()
        account = input("Paper account ID (DU...): ").strip().upper()
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", username) or not re.fullmatch(r"DU\d{5,}", account):
            raise ValueError("Invalid username or paper account ID.")
        secret = root / "secrets" / "tws_password"
        if not secret.exists():
            password = getpass.getpass("IBKR password (hidden): ")
            if not password:
                raise ValueError("A Gateway password is required.")
            secret.parent.mkdir(mode=0o700, exist_ok=True)
            private_file(secret, password)
        private_file(root / ".env", f"TWS_USERID={username}\nLAB_PAPER_ACCOUNT={account}\n")
    elif not (root / "secrets" / "tws_password").is_file():
        raise ValueError("Existing .env found, but secrets/tws_password is missing. Complete Gateway setup first.")
    for name in ("results", "research", "data"):
        (root / name).mkdir(exist_ok=True)
    env = dict(os.environ)
    env["LAB_APP_UID"] = str(os.getuid()) if hasattr(os, "getuid") else "1000"
    env["LAB_APP_GID"] = str(os.getgid()) if hasattr(os, "getgid") else "1000"
    subprocess.run(["docker", "compose", "-f", "compose.app.yml", "up", "--build", "-d"], check=True, cwd=root, env=env)
    print("Open http://127.0.0.1:8765 after Gateway login finishes. Complete any IBKR two-factor prompt.")
    print("Save your risk/capital choices, then click Start paper trading. No orders are enabled by this installer.")
    webbrowser.open("http://127.0.0.1:8765")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Setup stopped: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
