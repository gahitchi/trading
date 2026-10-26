"""Local setup and control UI. Never places orders outside the existing paper engine."""

import copy
import errno
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

from lab import exchange
from lab.agents.schedule import bar_freshness
from lab.authority import Authority, digest
from lab.config import load_config
from lab.paper import acquire_runtime_lock, runtime_dir
from lab.persistence import atomic_json, read_json
from lab.risk_profiles import PROFILES
from lab.safety import check_paper_settings, clear_kill_switch, read_kill_switch, set_kill_switch

STRATEGIES = {"trend": ["sma_trend"], "reversion": ["rsi_reversion"], "combined": ["sma_trend", "rsi_reversion"]}
CONNECTION_ENV = ("LAB_IB_HOST", "LAB_IB_PORT", "LAB_IB_CLIENT_ID", "LAB_PAPER_ACCOUNT")


def latest_completed_close(now, delay_minutes):
    from zoneinfo import ZoneInfo

    available = now - timedelta(minutes=delay_minutes)
    day = available.astimezone(ZoneInfo("America/New_York")).date()
    for _ in range(14):
        session = exchange.bounds(day)
        if session and session[1] <= available:
            return int(session[1].timestamp() * 1e9)
        day -= timedelta(days=1)
    raise ValueError("No supported completed market session.")


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def toml(raw):
    # Config contains one-level tables and JSON-compatible scalar/array values.
    def value(v):
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return json.dumps(v, ensure_ascii=True, allow_nan=False)

    return "\n".join(
        "[" + section + "]\n" + "\n".join(json.dumps(k) + " = " + value(v) for k, v in values.items()) + "\n"
        for section, values in raw.items()
    )


def validate_settings(s, universe):
    keys = {"host", "port", "account", "capital", "profile", "tickers", "strategy", "data", "mode", "hours"}
    if not isinstance(s, dict) or set(s) != keys:
        raise ValueError("Complete all setup fields.")
    if not isinstance(s["host"], str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,253}", s["host"]):
        raise ValueError("Enter the Gateway hostname or IPv4 address.")
    if type(s["port"]) is not int:
        raise ValueError("The Gateway port must be a number.")
    check_paper_settings(s["port"], s["account"])
    for key, low, high in (("capital", 1000, 1e9), ("hours", 0, 168)):
        if type(s[key]) not in (int, float) or not math.isfinite(s[key]) or not low <= s[key] <= high:
            raise ValueError(f"{key} must be between {low:g} and {high:g}.")
    if 0 < s["hours"] < 0.25:
        raise ValueError("A time limit must be at least 0.25 hours (0: no time limit).")
    if s["hours"] == 0 and s["mode"] != "autonomous":
        raise ValueError("Approving each order needs a time limit (0.25 to 168 hours).")
    if s["profile"] not in PROFILES or s["strategy"] not in STRATEGIES:
        raise ValueError("Choose a listed risk profile and strategy.")
    if s["data"] not in ("DELAYED_FROZEN", "REALTIME") or s["mode"] not in ("autonomous", "supervised"):
        raise ValueError("Choose a listed data feed and operating mode.")
    ts = s["tickers"]
    if not isinstance(ts, list) or not 1 <= len(ts) <= 8 or any(type(t) is not str for t in ts):
        raise ValueError("Choose one to eight supported symbols.")
    if len(set(ts)) != len(ts) or set(ts) - set(universe):
        raise ValueError("Choose each supported symbol only once.")
    return s


class Controller:
    def __init__(self, config):
        self.config_path = Path(config).resolve()
        self.cfg = load_config(self.config_path)
        self.base = copy.deepcopy(self.cfg.raw)
        self.directory = self.cfg.results_dir / "app"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings_path = self.directory / "settings.json"
        p = self.cfg.paper
        self.settings = {
            "host": p.host,
            "port": p.port,
            "account": p.account_id,
            "capital": p.capital,
            "profile": self.cfg.risk.profile if self.cfg.risk.profile in PROFILES else "balanced",
            "tickers": [t for t in ("SPY", "QQQ") if t in self.cfg.universe] or list(self.cfg.universe)[:2],
            "strategy": "combined",
            "data": p.market_data,
            "mode": "supervised",
            "hours": 24,  # unattended operation requires separately qualified research evidence
        }
        if self.settings_path.exists():
            record = read_json(self.settings_path)
            self.settings = validate_settings(record["settings"], self.cfg.universe)
            self._check_saved(record)
            self.cfg = load_config(self.config_path, environ={})
        self.lock = threading.RLock()
        self.process = None
        self.session = secrets.token_hex(16)
        self.phase, self.message = "setup", "Connect your paper account, choose your plan, then start."
        self.tail = deque(maxlen=100)
        self.deadline = 0
        self.closed = threading.Event()

    @property
    def rt(self):
        return runtime_dir(self.cfg)

    @property
    def reason(self):
        return "app:" + hashlib.sha256(str(self.config_path).encode()).hexdigest()

    def _check_saved(self, record=None):
        record = record or read_json(self.settings_path)
        if record.get("config_sha256") != hashlib.sha256(self.config_path.read_bytes()).hexdigest():
            raise ValueError("Saved setup and configuration differ. Review and save setup again before starting.")
        if record.get("settings") != self.settings:
            raise ValueError("Saved setup changed; restart the app before starting.")

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def configure(self, settings):
        with self.lock:
            s = validate_settings(copy.deepcopy(settings), self.cfg.universe)
            if self.alive():
                raise ValueError("Stop the app service before changing a saved trading plan.")
            if (self.rt / "identity.json").exists():
                old = {k: v for k, v in self.settings.items() if k not in ("mode", "hours")}
                new = {k: v for k, v in s.items() if k not in ("mode", "hours")}
                if old != new:
                    raise ValueError("This account already has trading history. A reviewed plan migration is required.")
            raw = copy.deepcopy(self.base)
            raw["paths"] = {
                "data": str(self.cfg.data_dir),
                "results": str(self.cfg.results_dir),
                "research": str(self.cfg.research_dir),
            }
            raw["risk"]["profile"] = s["profile"]
            raw.setdefault("trading", {}).update(speed="daily", data_delay_min=0 if s["data"] == "REALTIME" else 15)
            raw["paper"].update(
                host=s["host"],
                port=s["port"],
                account_id=s["account"],
                capital=s["capital"],
                market_data=s["data"],
                signal_time="16:20" if s["data"] == "DELAYED_FROZEN" else "16:10",
                rebalance_time="16:25" if s["data"] == "DELAYED_FROZEN" else "16:15",
            )
            content = toml(raw)
            # Validate before replacing the active config. Environment overrides
            # are captured in the initial form, never silently override that form.
            fd, name = tempfile.mkstemp(suffix=".toml", dir=self.config_path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                load_config(name, environ={})
            finally:
                os.unlink(name)
            runtime_lock = acquire_runtime_lock(self.cfg)
            try:
                atomic_text(self.config_path, content)
                atomic_json(
                    self.settings_path, {"settings": s, "config_sha256": hashlib.sha256(content.encode()).hexdigest()}
                )
                self.cfg = load_config(self.config_path, environ={})
                self.settings = s
                self.phase, self.message = (
                    "stopped",
                    "Plan saved. Start connects to IBKR and loads real historical bars.",
                )
            finally:
                runtime_lock.close()
        return self.status()

    def start(self):
        with self.lock:
            validate_settings(self.settings, self.cfg.universe)
            if not self.settings_path.exists():
                raise ValueError("Save your setup first.")
            self._check_saved()
            if (self.rt / "HALT").exists():
                raise ValueError("A hard halt requires reconciliation and review before restarting.")
            kill = read_kill_switch(self.rt / "KILL")
            if kill and kill.get("reason") != self.reason:
                raise ValueError("An existing stop must be reviewed and explicitly cleared with lab resume.")
            if kill and kill.get("mode") == "flatten":
                raise ValueError("An exit is in progress. Reconcile positions before clearing it with lab resume.")
            if self.settings["mode"] == "autonomous":
                from lab.research.approval import ApprovalError
                from lab.research.builtin_qualification import verify_builtin
                from lab.signals.registry import resolve

                names = STRATEGIES[self.settings["strategy"]]
                if len(names) != 1:
                    raise ValueError("autonomous ensembles need independent whole-policy qualification; use supervised")
                name = names[0]
                params = {"short": 1} if name == "sma_trend" and self.cfg.risk.max_short_gross > 0 else {}
                try:
                    verify_builtin(self.cfg, resolve(name, self.cfg.research_dir, params), self.settings["tickers"])
                except (ApprovalError, OSError, ValueError) as exc:
                    raise ValueError(
                        "autonomous paper requires qualified strategy evidence; use supervised or "
                        "lab research qualify-builtin"
                    ) from exc
            if self.alive() and self.phase in ("running", "preparing"):
                return self.status()
            if self.alive():
                # An explicit resume refreshes the connection, lease and data evidence;
                # it must not reuse stale heartbeat failures from the prior session.
                self._stop_engine()
            if not self.alive():
                lock = acquire_runtime_lock(self.cfg)
                try:
                    stop = set_kill_switch(self.rt / "KILL", "halt", self.reason, preserve_existing=True)
                    if stop.get("reason") != self.reason or stop.get("mode") != "halt":
                        raise ValueError("The stop state changed. Review it before starting.")
                finally:
                    lock.close()
                env = {k: v for k, v in os.environ.items() if k not in CONNECTION_ENV}
                self.session = secrets.token_hex(16)
                env["LAB_APP_SESSION"] = self.session
                args = [
                    sys.executable,
                    "-m",
                    "lab.cli",
                    "--config",
                    str(self.config_path),
                    "paper",
                    "--mode",
                    "supervised",
                    "--authority-hours",
                    str(self.settings["hours"]),
                    "--signals",
                    *STRATEGIES[self.settings["strategy"]],
                    "--tickers",
                    *self.settings["tickers"],
                ]
                if self.cfg.risk.max_short_gross > 0 and "sma_trend" in STRATEGIES[self.settings["strategy"]]:
                    args += ["--set", "sma_trend.short=1"]
                self.process = subprocess.Popen(
                    args,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                threading.Thread(target=self._logs, args=(self.process,), daemon=True).start()
            self.phase, self.message = (
                "preparing",
                "Connecting and loading history. Orders remain paused until all checks pass.",
            )
            self.deadline = time.monotonic() + 180
        return self.status()

    def _logs(self, process):
        handler = RotatingFileHandler(self.directory / "engine.log", maxBytes=5_000_000, backupCount=2)
        try:
            for line in process.stdout:
                clean = re.sub(r"\x1b\[[0-9;]*m", "", line.rstrip())[:4000]
                self.tail.append(clean)
                handler.emit(logging.LogRecord("engine", logging.INFO, "", 0, clean, (), None))
        finally:
            process.stdout.close()
            handler.close()

    def data_check(self):
        try:
            evidence = read_json(self.rt / "data-readiness.json")
            identity = read_json(self.rt / "identity.json")
            hb = read_json(self.rt / "heartbeat.json")
            if (
                evidence["session"] != self.session
                or evidence["source"] != "ibkr"
                or evidence["binding"] != digest(identity)
            ):
                return "Waiting for data from this broker session."
            age = (datetime.now(UTC) - datetime.fromisoformat(hb["at"])).total_seconds()
            if not 0 <= age < 120 or not hb["running"]:
                return "Waiting for a fresh engine heartbeat."
            if str(hb["account"]).split("-")[-1] != self.settings["account"]:
                return "Waiting for the configured paper account."
            if hb["hard_halt"] or hb["halted"] or (self.rt / "HALT").exists():
                return "A hard halt requires operator review."
            if hb.get("blocked"):
                return "Engine blocked: " + str(hb["blocked"])
            if evidence["errors"]:
                return "; ".join(evidence["errors"])
            expected = {f"{s}:{t}" for s in STRATEGIES[self.settings["strategy"]] for t in self.settings["tickers"]}
            if set(evidence["signals"]) != expected:
                return "Loading complete strategy history for every symbol."
            stamps = set()
            latest = latest_completed_close(datetime.now(UTC), self.cfg.trading.data_delay_min)
            for row in evidence["signals"].values():
                if (
                    not row["active"]
                    or bar_freshness(row["timestamp"], datetime.now(UTC))
                    or row["timestamp"] != latest
                ):
                    return "Waiting for current, completed market bars."
                stamps.add(row["timestamp"])
            if len(stamps) != 1:
                return "Waiting for aligned market bars across the portfolio."
            histories = [set(evidence["history"][t]) for t in self.settings["tickers"]]
            if len(set.intersection(*histories)) < self.cfg.portfolio.vol_window + 1:
                return "Loading aligned history for portfolio risk estimates."
            return None
        except (OSError, ValueError, KeyError, TypeError, OverflowError, AttributeError):
            return "Waiting for broker data and account checks."

    def tick(self):
        with self.lock:
            if self.process is not None and not self.alive():
                self.phase, self.message = (
                    "stopped",
                    "Engine stopped. Check the connection and the engine log, then retry.",
                )
                return
            if self.phase == "running":
                try:
                    Authority(self.rt, read_json(self.rt / "identity.json")).read()
                    hb = read_json(self.rt / "heartbeat.json")
                    age = (datetime.now(UTC) - datetime.fromisoformat(hb["at"])).total_seconds()
                    if not 0 <= age < 120 or not hb["running"] or hb.get("blocked") or hb["hard_halt"] or hb["halted"]:
                        raise ValueError("The engine needs attention.")
                    if read_kill_switch(self.rt / "KILL"):
                        raise ValueError("A stop is active.")
                except (OSError, ValueError, KeyError, TypeError):
                    self.pause()
                    self.phase, self.message = (
                        "blocked",
                        "Authority expired or engine checks failed. Review status, then explicitly Start again.",
                    )
                return
            if self.phase != "preparing":
                return
            issue = self.data_check()
            if issue:
                self.message = issue
                if time.monotonic() >= self.deadline:
                    self.phase = "blocked"
                    self.message = issue + " Check Gateway login and data permissions, then retry Start."
                return
            kill = read_kill_switch(self.rt / "KILL")
            if not kill or kill.get("reason") != self.reason or kill.get("mode") != "halt":
                self.phase, self.message = "blocked", "The stop state changed. Review it before restarting."
                return
            try:
                Authority(self.rt, read_json(self.rt / "identity.json")).issue(
                    self.settings["mode"], self.settings["hours"]
                )
                clear_kill_switch(self.rt / "KILL", expected=kill)
                self.phase, self.message = (
                    "running",
                    "Plan active. Decisions follow the daily schedule; orders are not immediate.",
                )
            except (OSError, ValueError):
                self.phase, self.message = "blocked", "Could not activate authority. Trading remains paused."

    def pause(self, flatten=False):
        with self.lock:
            # A routine pause or shutdown must not erase an external stop or downgrade an exit.
            stop = set_kill_switch(
                self.rt / "KILL", "flatten" if flatten else "halt", self.reason, preserve_existing=True
            )
            if flatten and stop.get("mode") != "flatten":
                raise ValueError("An external stop is active. Review it before requesting an exit.")
            self.phase = "paused"
            self.message = (
                "Stop requested. Open orders cancel on the heartbeat; existing positions remain."
                if not flatten
                else "Exit requested. Positions close when the engine and market permit; verify fills."
            )
        return self.status()

    def approve(self, ident):
        with self.lock:
            if self.phase != "running" or self.settings["mode"] != "supervised":
                raise ValueError("Approvals require an active supervised plan.")
            Authority(self.rt, read_json(self.rt / "identity.json")).approve(ident)
        return self.status()

    def offline_demo(self, options):
        """Run a bounded, credential-free *synthetic* reference scenario.

        This does not start a service, enable a venue, or touch the saved
        IBKR account configuration, execution authority or hard-stop files.
        """
        from decimal import Decimal
        from importlib.resources import files

        from lab.markets.__main__ import demo
        from lab.markets.domain import Side

        if type(options) is not dict or set(options) != {"side", "quantity", "capital"}:
            raise ValueError("Expected only side, quantity and capital for the offline sample.")
        if any(type(value) is not str or len(value) > 64 for value in options.values()):
            raise ValueError("Offline parameters must be short strings.")
        if options["side"] not in ("buy", "sell"):
            raise ValueError("Offline side must be buy or sell.")
        try:
            quantity, capital = Decimal(options["quantity"]), Decimal(options["capital"])
        except ArithmeticError as exc:
            raise ValueError("Offline quantity and capital must be finite decimals.") from exc
        if not quantity.is_finite() or not capital.is_finite():
            raise ValueError("Offline quantity and capital must be finite decimals.")
        if not Decimal("0.001") <= quantity <= Decimal("2"):
            raise ValueError("Offline demonstration quantity must be 0.001..2 contracts.")
        if not Decimal("100") <= capital <= Decimal("1000000"):
            raise ValueError("Offline demonstration capital must be 100..1000000.")
        with self.lock:
            if self.alive():
                raise ValueError("Pause the broker engine before running an independent offline sandbox.")
        path = Path(str(files("lab.markets").joinpath("fixture.csv")))
        return {
            "mode": "offline_sim",
            "sample_kind": "synthetic_fixture",
            "broker_connected": False,
            "orders_routed": False,
            "strategy_qualified": False,
            "result": demo(path, quantity, Side(options["side"]), capital),
        }

    def public_crypto_snapshot(self, options):
        """Explicitly requested, read-only EEA public market inspection.

        The only accepted input is a vetted instrument ID and bounded depth;
        the implementation cannot change URLs, use credentials or route orders.
        Never confuse indicative public funding with a settled broker debit.
        """
        from lab.markets.preview import public_preview

        if type(options) is not dict or set(options) != {"instrument", "depth"}:
            raise ValueError("Expected only instrument and depth for public market inspection.")
        instrument, depth = options["instrument"], options["depth"]
        if type(instrument) is not str or not re.fullmatch(r"[A-Z0-9_-]{3,80}-SWAP", instrument):
            raise ValueError("Public funding inspection requires a canonical swap instrument.")
        if type(depth) is not str or depth not in {"1", "5", "10"}:
            raise ValueError("Public order-book depth must be 1, 5, or 10.")
        return public_preview(instrument, depth=int(depth))

    def status(self):
        with self.lock:
            hb, requests, authority = {}, [], None
            try:
                hb = read_json(self.rt / "heartbeat.json")
                control = Authority(self.rt, read_json(self.rt / "identity.json"))
                authority = control.read(allow_expired=True)
                if self.settings["mode"] == "supervised":
                    requests = control.requests()
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                pass
            try:
                snapshot_age = (datetime.now(UTC) - datetime.fromisoformat(hb["at"])).total_seconds()
                snapshot_current = self.alive() and hb["running"] and 0 <= snapshot_age < 120
            except (ValueError, KeyError, TypeError):
                snapshot_current = False
            return {
                "schedule": {"signal": self.cfg.paper.signal_time, "decision": self.cfg.paper.rebalance_time},
                "snapshot_current": snapshot_current,
                "phase": self.phase,
                "message": self.message,
                "settings": self.settings,
                "symbols": list(self.cfg.universe),
                "heartbeat": hb,
                "authority": authority,
                "requests": requests,
                "engine_alive": self.alive(),
                "log": list(self.tail)[-12:],
                "data_issue": self.data_check() if self.alive() else None,
            }

    def close(self):
        self.closed.set()
        self._stop_engine()

    def _stop_engine(self):
        if self.alive():
            self.pause()
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
            self.process = None


UI = Path(__file__).parent / "ui"
# Every path the app serves, fixed: nothing from the request reaches the filesystem.
UI_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript"),
    "/app.css": ("app.css", "text/css"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icons/icon.svg": ("icons/icon.svg", "image/svg+xml"),
    "/icons/icon-32.png": ("icons/icon-32.png", "image/png"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/fonts/archivo-latin.woff2": ("fonts/archivo-latin.woff2", "font/woff2"),
    "/fonts/archivo-latin-ext.woff2": ("fonts/archivo-latin-ext.woff2", "font/woff2"),
}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller):
        super().__init__(address, Handler)
        self.controller = controller
        self.token = secrets.token_hex(32)
        port = self.server_address[1]
        self.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def reply(self, code, body, content_type="application/json"):
        if isinstance(body, bytes):
            raw = body
        else:
            raw = body.encode() if isinstance(body, str) else json.dumps(body, allow_nan=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # Browsers may abandon in-flight status polls while the local app
            # is suspended, refreshed or closing. They do not affect trading.
            # Suppress only transport disconnects, never application failures.
            self.close_connection = True

    def allowed(self):
        return self.headers.get("Host") in self.server.hosts

    def token_ok(self):
        value = self.headers.get("X-App-Token", "")
        return value.isascii() and hmac.compare_digest(value, self.server.token)

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, {"error": "Local access only."})
        if self.path == "/health":
            return self.reply(200, {"ok": True})
        if self.path == "/api/status":
            if not self.token_ok():
                return self.reply(403, {"error": "Reload the app."})
            return self.reply(200, self.server.controller.status())
        if self.path not in UI_FILES:
            return self.reply(404, {"error": "Not found."})
        name, kind = UI_FILES[self.path]
        body = (UI / name).read_bytes()
        if self.path == "/":
            body = body.replace(b"APP_TOKEN", self.server.token.encode())
        return self.reply(200, body, kind)

    def do_POST(self):
        if (
            not self.allowed()
            or not self.token_ok()
            or self.headers.get("Origin") != "http://" + self.headers.get("Host", "")
            or self.headers.get("Content-Type") != "application/json"
        ):
            return self.reply(403, {"error": "Request refused. Reload the local app."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Expected a settings object.")
            c = self.server.controller
            if self.path == "/api/setup":
                result = c.configure(body)
            elif self.path == "/api/start":
                result = c.start()
            elif self.path == "/api/pause":
                result = c.pause()
            elif self.path == "/api/exit":
                if body.get("confirm") is not True:
                    raise ValueError("Confirm the position exit.")
                result = c.pause(flatten=True)
            elif self.path == "/api/approve":
                result = c.approve(body.get("request"))
            elif self.path == "/api/offline-demo":
                result = c.offline_demo(body)
            elif self.path == "/api/public-crypto":
                result = c.public_crypto_snapshot(body)
            else:
                return self.reply(404, {"error": "Not found."})
            return self.reply(200, result)
        except Exception as exc:
            # Fail the UI action without stopping the independent engine/kill switch.
            return self.reply(400, {"error": str(exc)})


def serve(args):
    import fcntl

    directory = Path(args.directory).expanduser().resolve()
    if args.host == "0.0.0.0" and not Path("/.dockerenv").exists():
        raise ValueError("Listening on all interfaces is reserved for the loopback-published Docker app.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = Path(args.config).resolve() if args.config else directory / "lab.toml"
    if not config.exists():
        atomic_text(config, (Path(__file__).parent / "default.toml").read_text())
    controller = Controller(config)
    with (controller.directory / "LOCK").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            server = AppServer((args.host, args.port), controller)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:  # do not interrupt an existing app instance
                raise OSError(
                    exc.errno,
                    f"Trading Lab cannot bind {args.host}:{args.port}: port in use. "
                    "Identify the listener or select another port with --port.",
                ) from exc
            raise

        def monitor():
            while not controller.closed.wait(1):
                try:
                    controller.tick()
                except Exception:
                    try:
                        controller.pause()
                    except Exception:
                        logging.exception("Could not persist stop request; stopping the engine")
                        if controller.alive():
                            os.killpg(controller.process.pid, signal.SIGINT)
                    controller.phase, controller.message = (
                        "blocked",
                        "A control check failed. Inspect the local engine log.",
                    )

        threading.Thread(target=monitor, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        print(f"Trading Lab: {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

        def terminate(*_):
            raise KeyboardInterrupt

        previous_term = signal.signal(signal.SIGTERM, terminate)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            controller.close()
            signal.signal(signal.SIGTERM, previous_term)
    return 0
