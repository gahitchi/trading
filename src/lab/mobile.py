"""Trading Lab pocket companion: offline diagnostics, never broker execution.

Native Termux is an Android/Bionic platform and is not a supported Nautilus
engine runtime. Keep this module standard-library only, independent of the
production CLI and deliberately unable to submit orders or load credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import platform
import sqlite3
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lab.execution_state import validate_ledger
from lab.persistence import read_json

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
BIND_HOST = "127.0.0.1"


class MobileDiagnosticError(ValueError):
    """A snapshot or runtime requirement cannot be verified on this device."""


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def doctor() -> dict:
    """Report capabilities without probing accounts, networks or credentials."""
    return {
        "application": "Trading Lab",
        "mode": "OFFLINE_DIAGNOSTIC_ONLY",
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "git_checkout": (root() / ".git").exists(),
        "native_nautilus_execution_qualified": False,
        "broker_connection_enabled": False,
        "order_submission_enabled": False,
        "live_money_enabled": False,
        "available": ["portable reference invariants", "read-only exported execution-state inspection"],
        "not_available": ["Nautilus backtest", "IBKR/OKX adapter", "autonomous trading", "release qualification"],
    }


def _require_snapshot(path: str | Path) -> Path:
    file = Path(path).expanduser()
    if file.is_symlink():
        raise MobileDiagnosticError("snapshot symlink not accepted; inspect an isolated export")
    if not file.is_file():
        raise MobileDiagnosticError("snapshot path must be an existing regular file")
    if file.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise MobileDiagnosticError("snapshot too large for portable bounded inspection")
    if file.suffix not in {".json", ".sqlite3"}:
        raise MobileDiagnosticError("expected exported ledger .json or .sqlite3")
    return file


def _sqlite_snapshot(file: Path) -> dict:
    # Read the *copy*, never instantiate ExecutionStore: that constructor
    # initializes/mutates the database and belongs solely to the full runtime.
    conn = sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise MobileDiagnosticError("unrecognized execution SQLite schema version")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MobileDiagnosticError("SQLite integrity check failed")
        row = conn.execute("SELECT value FROM snapshot WHERE id=1").fetchone()
        if row is None:
            raise MobileDiagnosticError("authoritative execution snapshot is missing")
        def unique(pairs):
            out = {}
            for k, v in pairs:
                if k in out:
                    raise MobileDiagnosticError("duplicate execution snapshot JSON key")
                out[k] = v
            return out
        def no_nonfinite(value):
            raise MobileDiagnosticError(f"nonfinite execution JSON number: {value}")
        state = json.loads(row[0], object_pairs_hook=unique, parse_constant=no_nonfinite)
        if not isinstance(state, dict) or state.get("authoritative_store") != file.name:
            raise MobileDiagnosticError("execution snapshot is not bound to this database name")
        return state
    finally:
        conn.close()


def inspect_snapshot(path: str | Path) -> dict:
    """Validate an isolated state export, show counts without leaking identifiers.

    Do not use this on an active broker runtime, nor treat passing validation
    as authentication against actual brokerage positions or pending orders.
    """
    file = _require_snapshot(path)
    try:
        state = _sqlite_snapshot(file) if file.suffix == ".sqlite3" else read_json(file)
        validate_ledger(state)
    except (OSError, ValueError, sqlite3.Error, TypeError, KeyError) as exc:
        raise MobileDiagnosticError(f"cannot verify exported execution state: {exc}") from exc
    # Hash only a bounded, isolated artifact. A valid checksum proves local
    # byte identity, not external broker reconciliation or tamper resistance.
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    return {
        "mode": "OFFLINE_DIAGNOSTIC_ONLY",
        "artifact_sha256": digest,
        "format": "sqlite_snapshot" if file.suffix == ".sqlite3" else "json_projection",
        "schema_valid": True,
        "authoritative": file.suffix == ".sqlite3",
        "broker_reconciled": False,
        "execution_permitted": False,
        "positions_recorded": sum(bool(qty) for qty in state.get("positions", {}).values()),
        "pending_orders_recorded": len(state.get("pending", {})),
        "applied_trades_in_snapshot": len(state.get("applied_trades", [])),
        "warning": "Local state only; confirm broker identity, open orders and native account independently",
    }


def check() -> bool:
    tests_dir = root() / "tests"
    if not tests_dir.is_dir():
        raise MobileDiagnosticError("portable source tests unavailable; use the Git checkout")
    # Restrict collection: the general test suite imports native Nautilus,
    # NumPy and pandas and is not supported by Android Bionic Python.
    suite = unittest.TestLoader().discover(str(tests_dir), pattern="test_portable_*.py")
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def _page(snapshot: dict | None) -> bytes:
    report = doctor()
    items = [
        ("Execution authority", "DISABLED — offline diagnostics only"),
        ("Python", report["python"]),
        ("Architecture", report["architecture"]),
        ("Engine", "Unavailable in native Termux; no simulated broker attached"),
    ]
    if snapshot:
        items.extend(
            [
                ("Snapshot", snapshot["format"]),
                ("Local schema", "PASS (not broker reconciliation)"),
                ("Positions", str(snapshot["positions_recorded"])),
                ("Pending orders", str(snapshot["pending_orders_recorded"])),
                ("Snapshot SHA-256", snapshot["artifact_sha256"]),
            ]
        )
    lines = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
        for label, value in items
    )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='dark'>"
        "<title>Trading Lab — Pocket Diagnostics</title>"
        "<style>body{background:#10141c;color:#f2f4f8;font:16px system-ui,sans-serif;"
        "margin:0 auto;max-width:700px;padding:24px}"
        "h1{font-size:1.55rem}p{color:#bcc9db;line-height:1.6}"
        "dl{display:grid;grid-template-columns:1fr 1.35fr;gap:1px;background:#26354b;"
        "border:1px solid #26354b;border-radius:12px;overflow:hidden}"
        "dt,dd{background:#18202e;padding:15px;margin:0;overflow-wrap:anywhere}"
        "dt{color:#bfccdd}footer{font-size:.85rem;color:#afbac9;margin-top:24px}</style>"
        "<h1>Trading Lab · Pocket Diagnostics</h1>"
        "<p>Read-only mobile diagnostics. No market data, no broker connection, no order submission.</p>"
        f"<dl>{lines}</dl>"
        "<footer>Only separately exported snapshots can be inspected. Local integrity is not broker parity. "
        "Live money remains disabled. <a href='/status'>Status JSON</a></footer></html>"
    ).encode()


def serve(*, port: int, snapshot: str | None = None) -> None:
    if not 0 <= port <= 65535:
        raise MobileDiagnosticError("port must be 0–65535")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/status"):
                self.send_error(404, "Not found")
                return
            try:
                state = inspect_snapshot(snapshot) if snapshot else None
            except MobileDiagnosticError:
                self.send_error(422, "Snapshot verification failed")
                return
            payload = (
                _page(state)
                if self.path == "/"
                else json.dumps({"doctor": doctor(), "snapshot": state}, allow_nan=False).encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if self.path == "/" else "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            self.send_error(405, "Mobile diagnostics are read-only")

    with ThreadingHTTPServer((BIND_HOST, port), Handler) as server:
        print(f"Pocket diagnostics: http://{BIND_HOST}:{server.server_port}/", flush=True)
        server.serve_forever(poll_interval=0.5)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m lab.mobile", description="Offline-only Termux companion")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Show portable-only capability boundaries")
    sub.add_parser("check", help="Run existing stdlib portable reference tests")
    audit = sub.add_parser("inspect", help="Validate an isolated exported ledger (read-only)")
    audit.add_argument("snapshot", help="Exported .json or .sqlite3; never an active broker runtime")
    server = sub.add_parser("serve", help="Serve a read-only phone dashboard on 127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--snapshot", help="Optional isolated exported ledger")
    args = p.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(doctor(), indent=2))
        elif args.command == "check":
            return 0 if check() else 1
        elif args.command == "inspect":
            print(json.dumps(inspect_snapshot(args.snapshot), indent=2))
        else:
            serve(port=args.port, snapshot=args.snapshot)
        return 0
    except (MobileDiagnosticError, KeyboardInterrupt) as exc:
        print(f"mobile diagnostics: {exc or 'stopped'}", file=sys.stderr)
        return 2 if isinstance(exc, MobileDiagnosticError) else 130


if __name__ == "__main__":
    raise SystemExit(main())
