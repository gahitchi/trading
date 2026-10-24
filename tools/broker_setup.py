"""Local-only, optional IBKR PAPER gateway setup. Never issues trading authority.

Only the trusted Trading Lab page at http://127.0.0.1:8765 may submit
credentials. Runs as the desktop user, outside Docker, without Docker socket mounts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8766
PAGE_ORIGIN = "http://127.0.0.1:8765"
USER_RE = re.compile(r"[A-Za-z0-9_.@-]{1,100}\Z")
PAPER_RE = re.compile(r"DU[0-9]{5,}\Z")
DEMO_RE = re.compile(r"DUR[0-9]{5,}\Z")
MAX_INPUT = 4096


def private_file(path: Path, value: str) -> None:
    """Fail closed if the target exists, including symbolic links."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def saved_account(root: Path) -> str | None:
    env = root / ".env"
    secret = root / "secrets" / "tws_password"
    if not env.is_file() or not secret.is_file():
        return None
    # No passwords or username are returned to the UI.
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("LAB_PAPER_ACCOUNT="):
            account = line.split("=", 1)[1].strip()
            if PAPER_RE.fullmatch(account) or DEMO_RE.fullmatch(account):
                return account
    return None


def start_gateway(root: Path) -> bool:
    """Only create the paper Gateway. Never restart the GUI or start the engine."""
    account = saved_account(root)
    if not account:
        return False
    # Trial/demo credentials never start an order-enabled Gateway.
    overlay = ["-f", "compose.gateway.demo.yml"] if DEMO_RE.fullmatch(account) else []
    try:
        process = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "compose.app.yml",
                "-f",
                "compose.gateway.yml",
                *overlay,
                "up",
                "-d",
                "--no-deps",
                "ib-gateway",
            ],
            cwd=root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        return process.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def save_credentials(root: Path, payload: dict) -> str:
    if saved_account(root) or (root / ".env").exists() or (root / "secrets" / "tws_password").exists():
        raise ValueError("Paper credentials are already stored; use Retry Gateway or review the local setup files.")
    if not isinstance(payload, dict) or set(payload) != {"username", "account", "password"}:
        raise ValueError("Supply broker-login username, separate simulation account number and password.")
    username, account, password = payload["username"], payload["account"], payload["password"]
    if not isinstance(username, str) or not USER_RE.fullmatch(username):
        raise ValueError("Invalid username.")
    if not isinstance(account, str) or not (PAPER_RE.fullmatch(account) or DEMO_RE.fullmatch(account)):
        raise ValueError("Only DU paper account numbers or DUR Free Trial simulation numbers are supported.")
    if (
        not isinstance(password, str)
        or not 1 <= len(password) <= 1024
        or any(c in password for c in ("\r", "\n", "\x00"))
    ):
        raise ValueError("Invalid password.")
    secret_dir = root / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    if secret_dir.is_symlink():
        raise ValueError("Refusing symlinked secrets directory.")
    os.chmod(secret_dir, 0o700)
    secret = secret_dir / "tws_password"
    private_file(secret, password)
    try:
        private_file(root / ".env", f"TWS_USERID={username}\nLAB_PAPER_ACCOUNT={account}\n")
    except Exception:
        secret.unlink(missing_ok=True)
        raise
    return account


class SetupHandler(BaseHTTPRequestHandler):
    server: SetupServer

    def log_message(self, *args):
        # Never log POST payloads, passwords, account IDs or request URIs.
        pass

    def _trusted(self) -> bool:
        return (
            self.client_address[0] == HOST
            and self.headers.get("Host") == f"{HOST}:{PORT}"
            and self.headers.get("Origin") == PAGE_ORIGIN
        )

    def reply(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if self._trusted():
            self.send_header("Access-Control-Allow-Origin", PAGE_ORIGIN)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        if not self._trusted() or self.path not in ("/connect", "/retry"):
            return self.reply(403, {"error": "Only the local Trading Lab GUI is allowed."})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", PAGE_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._trusted() or self.path != "/status":
            return self.reply(403, {"error": "Only the local Trading Lab GUI is allowed."})
        account = saved_account(self.server.root)
        return self.reply(
            200,
            {
                "configured": account is not None,
                "account": account,
                "mode": "demo" if account and DEMO_RE.fullmatch(account) else "paper",
                "order_setup_enabled": bool(account and PAPER_RE.fullmatch(account)),
                "installation": hashlib.sha256(str(self.server.root).encode("utf-8")).hexdigest(),
            },
        )

    def do_POST(self):
        if not self._trusted() or self.path not in ("/connect", "/retry"):
            return self.reply(403, {"error": "Only the local Trading Lab GUI is allowed."})
        if self.headers.get("Content-Type") != "application/json":
            return self.reply(415, {"error": "JSON required."})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_INPUT:
            return self.reply(400, {"error": "Invalid request size."})
        try:
            body = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, ValueError):
            return self.reply(400, {"error": "Malformed JSON."})
        with self.server.setup_lock:
            try:
                if self.path == "/connect":
                    account = save_credentials(self.server.root, body)
                else:
                    if body != {}:
                        raise ValueError("Retry requires an empty request.")
                    account = saved_account(self.server.root)
                    if not account:
                        raise ValueError("No valid paper credentials stored.")
                ready = start_gateway(self.server.root)
            except ValueError as exc:
                return self.reply(400, {"error": str(exc)})
            except OSError:
                return self.reply(500, {"error": "Could not store the private broker configuration."})
        if not ready:
            return self.reply(
                503, {"error": "Credentials saved, but Gateway startup failed. Check Docker and Retry Gateway."}
            )
        return self.reply(
            200,
            {
                "configured": True,
                "account": account,
                "mode": "demo" if DEMO_RE.fullmatch(account) else "paper",
                "order_setup_enabled": bool(PAPER_RE.fullmatch(account)),
            },
        )


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root: Path):
        super().__init__(address, SetupHandler)
        self.root = root.resolve()
        self.setup_lock = threading.Lock()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Loopback-only Trading Lab paper broker configuration.")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / "compose.app.yml").is_file() or not (root / "compose.gateway.yml").is_file():
        raise ValueError("Not a Trading Lab installation directory.")
    server = SetupServer((HOST, PORT), root)
    server.serve_forever()


if __name__ == "__main__":
    main()
