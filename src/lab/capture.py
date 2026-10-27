"""
Read-only evidence capture for the supervised IBKR paper session (C004, docs/paper-acceptance.md).

Three steps, none of which can place, modify or cancel an order:

  snapshot  connect to the broker with a node that holds a single DataActor (no Strategy, so no order
            factory exists), let Nautilus reconcile, then record what the broker reports: account
            balances, positions, every order with its events (fills, trade IDs, commissions), and
            raw bars exactly as delivered (ts_event, ts_init, local receipt time) next to the lab's
            normalized timestamps. Requires the paper node to be stopped (it takes the runtime lock).
  bundle    copy the lab's own runtime records (journal, ledger store, smoke reports, heartbeat,
            halt/recovery audit, notification outbox) for the same phase.
  sanitize  write a redacted copy for the repository: account IDs and broker order/trade IDs are
            pseudonymized consistently (HMAC with a key kept outside Git, so joins across phases
            still line up), secrets are dropped, and a manifest records the SHA-256 of every original
            and redacted file plus the redaction procedure.

Originals stay outside the repository: the tool refuses to write raw captures inside the Git working
tree. Every capture carries an identity record (commit, dirty flag, lock hash, config hash, strategy
digests, calendar version, Python and package versions, data delay and speed).

Nothing here grades a scenario. A capture is evidence for both reviewers to judge against B01-B10; the
tool never reports a case as passed.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from lab.config import LabConfig

GRADING_NOTE = (
    "captured only: grading is by both reviewers against docs/paper-acceptance.md; "
    "a capture file existing is not a pass"
)


# ----------------------------------------------------------------------------- identity and paths


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def repo_root(cfg: LabConfig) -> Path | None:
    top = _git(cfg.root, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def check_outside_repo(cfg: LabConfig, out: Path) -> Path:
    """Raw captures can hold account numbers: they never go inside the Git working tree."""
    out = out.expanduser().resolve()
    root = repo_root(cfg) or cfg.root.resolve()
    if out == root or root in out.parents:
        raise ValueError(
            f"{out} is inside the repository ({root}); keep raw broker captures in a private evidence store "
            "outside Git (e.g. ~/trading-evidence) and commit only `sanitize` output"
        )
    return out


def identity(cfg: LabConfig, specs=()) -> dict:
    """What produced this evidence: freeze it before the session (docs/paper-acceptance.md)."""
    from importlib import metadata

    root = repo_root(cfg)
    lock = (root or cfg.root) / "uv.lock"
    config = cfg.root / "lab.toml"
    try:
        import exchange_calendars

        calendar = exchange_calendars.__version__
    except ImportError:
        calendar = None
    packages = {}
    for name in ("nautilus_trader", "pandas", "numpy", "exchange-calendars", "yfinance"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _git(root, "rev-parse", "HEAD") if root else None,
        "dirty": bool(_git(root, "status", "--porcelain")) if root else None,
        "lock_sha256": _sha256(lock) if lock.exists() else None,
        "config_sha256": _sha256(config) if config.exists() else None,
        "strategies": [{"name": s.name, "params": s.params, "sha256": getattr(s, "sha256", None)} for s in specs],
        "calendar_version": calendar,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": packages,
        "profile": cfg.risk.profile,
        "speed": cfg.trading.speed,
        "data_delay_min": cfg.trading.data_delay_min,
        "market_data": cfg.paper.market_data,
        "paper_port": cfg.paper.port,
        "session_time_zone": "America/New_York",
    }


def phase_dir(out: Path, phase: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", phase):
        raise ValueError("phase: letters, digits, '.', '_' or '-', e.g. B03-daily-open")
    d = out / phase
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str))
    os.replace(tmp, path)


_CAPTURE_REQUIRED = (
    "identity.json",
    "broker_account.json",
    "broker_positions.json",
    "broker_orders.json",
    "broker_bars.json",
    "broker_capture_meta.json",
)


def _set_capture_status(folder: Path, state: str) -> None:
    """Explicitly quarantine failures; even a completed cache is NOT authenticated."""
    _write(
        folder / "CAPTURE_STATUS.json",
        {"state": state, "broker_authenticated": False, "history_coverage_verified": False},
    )


def _verify_capture_files(folder: Path, *, expected_account: str, expected_port: int, tickers: list[str]) -> None:
    """Check local cache coherence before committing status COMPLETE_UNVERIFIED.

    This never authenticates the broker or asserts historical coverage.
    """
    if any((folder / name).is_symlink() or not (folder / name).is_file() for name in _CAPTURE_REQUIRED):
        raise RuntimeError("capture incomplete: required broker snapshot files missing")

    def record(name: str) -> object:
        path = folder / name
        if path.stat().st_size > 8 * 1024 * 1024:
            raise RuntimeError("capture incomplete: broker evidence exceeds B01 inspection limit")
        try:
            return json.loads(
                path.read_text(),
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
            )
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("capture incomplete: invalid broker evidence JSON") from exc

    account = record("broker_account.json")
    positions = record("broker_positions.json")
    orders = record("broker_orders.json")
    meta = record("broker_capture_meta.json")
    identity_record = record("identity.json")
    if not isinstance(account, dict) or account.get("account_id") != "IB-" + expected_account:
        raise RuntimeError("capture incomplete: broker account observation missing or mismatched")
    if (
        not isinstance(identity_record, dict)
        or type(identity_record.get("paper_port")) is not int
        or identity_record["paper_port"] != expected_port
    ):
        raise RuntimeError("capture incomplete: pinned paper gateway port not observed")
    balances = account.get("balances")
    if not isinstance(balances, list) or not balances:
        raise RuntimeError("capture incomplete: broker balances not observed")
    for balance in balances:
        if not isinstance(balance, dict) or not isinstance(balance.get("currency"), str):
            raise RuntimeError("capture incomplete: malformed broker currency balance")
        for field in ("total", "free", "locked"):
            value = balance.get(field)
            if type(value) is not str or len(value) > 100:
                raise RuntimeError("capture incomplete: missing exact broker currency balance")
            try:
                if not Decimal(value).is_finite():
                    raise RuntimeError("capture incomplete: nonfinite broker currency balance")
            except ArithmeticError as exc:
                raise RuntimeError("capture incomplete: invalid exact broker currency balance") from exc
    if not isinstance(positions, dict) or not tickers or not all(
        isinstance(positions.get(t), dict)
        and isinstance(positions[t].get("positions"), list)
        and isinstance(positions[t].get("net_exact"), str)
        for t in tickers
    ):
        raise RuntimeError("capture incomplete: broker positions not observed for requested instruments")
    if not isinstance(orders, list) or any(not isinstance(order, dict) for order in orders):
        raise RuntimeError("capture incomplete: broker order cache malformed or missing")
    if not isinstance(meta, dict) or meta.get("stream") != "NAUTILUS_IBKR_READ_ONLY":
        raise RuntimeError("capture incomplete: broker observation metadata missing")
    start, end = meta.get("capture_started_ns"), meta.get("capture_completed_ns")
    if type(start) is not int or type(end) is not int or not 0 < start <= end:
        raise RuntimeError("capture incomplete: broker observation timestamps missing")
    if any(
        meta.get(k) is not False
        for k in ("coverage_verified", "broker_authentication_verified", "causal_boundary_verified")
    ):
        raise RuntimeError("capture incomplete: unsupported source coverage assertion")


# ----------------------------------------------------------------------------- snapshot (read-only node)


def _bar_record(bar, received_ns: int, normalized_ns: int | None) -> dict:
    def iso(ns):
        return datetime.fromtimestamp(ns / 1e9, UTC).isoformat(timespec="microseconds")

    return {
        "bar_type": str(bar.bar_type),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "ts_event": int(bar.ts_event),
        "ts_event_utc": iso(bar.ts_event),
        "ts_init": int(bar.ts_init),
        "ts_init_utc": iso(bar.ts_init),
        "received_utc": iso(received_ns),
        "lab_normalized_ts_event_utc": iso(normalized_ns) if normalized_ns is not None else None,
    }


def _order_record(order) -> dict:
    events = []
    for e in order.events():
        rec = {"type": type(e).__name__, "ts_event": int(e.ts_event), "ts_init": int(e.ts_init)}
        for attr in ("trade_id", "last_qty", "last_px", "commission", "reason", "venue_order_id", "liquidity_side"):
            if hasattr(e, attr):
                rec[attr] = str(getattr(e, attr))
        events.append(rec)
    return {
        "client_order_id": str(order.client_order_id),
        "venue_order_id": str(order.venue_order_id) if order.venue_order_id is not None else None,
        "instrument_id": str(order.instrument_id),
        "side": str(order.side),
        "order_type": str(order.order_type),
        "time_in_force": str(order.time_in_force),
        "status": str(order.status),
        "quantity": str(order.quantity),
        "filled_qty": str(order.filled_qty),
        "price": str(getattr(order, "price", None)),
        "is_closed": bool(order.is_closed),
        "ts_init": int(order.ts_init),
        "ts_last": int(order.ts_last),
        "events": events,
    }


def _make_actor(cfg: LabConfig, iids: dict, out_dir: Path, wait_s: float, history_days: int):
    from nautilus_trader.common import DataActor, DataActorConfig

    from lab import intraday
    from lab.agents.broker_bars import normalize_ib_bar
    from lab.agents.schedule import Session
    from lab.universe import bar_type

    sp = intraday.speed(cfg.trading.speed)
    session = None if sp.daily else Session(sp.minutes, delay_min=cfg.trading.data_delay_min)
    specs = ["1-DAY"] if sp.daily else ["1-DAY", sp.bar_spec()]

    class CaptureConfig(DataActorConfig):
        def __init__(self, **kw):
            super().__init__(**kw)

    class CaptureActor(DataActor):
        """Records what the broker reports. A DataActor has no order factory: it cannot trade."""

        def __init__(self):
            super().__init__(CaptureConfig())
            self.bars: list[dict] = []
            self.done = False
            self.started_ns: int | None = None

        def on_start(self):
            self.started_ns = time.time_ns()
            start = datetime.now(UTC) - timedelta(days=history_days)
            for iid in iids.values():
                for spec in specs:
                    self.request_bars(bar_type(iid, spec), start=start)
            at = self.clock.timestamp_ns() + int(wait_s * 1e9)
            self.clock.set_time_alert_ns("capture-dump", at, callback=self._dump)

        def on_historical_bars(self, bars):
            received = time.time_ns()
            for b in bars:
                daily = "-1-DAY-" in str(b.bar_type)
                try:
                    norm = normalize_ib_bar(b, None if daily else session).ts_event
                except Exception:  # record the raw bar even if the lab's normalization refuses it
                    norm = None
                self.bars.append(_bar_record(b, received, norm))

        def _dump(self, _event):
            if self.done:
                return
            self.done = True
            # Capture interval starts when the actor requests the broker observations,
            # not when the timer begins writing the final cache snapshot.
            capture_started_ns = self.started_ns
            if capture_started_ns is None:
                raise RuntimeError("capture actor never entered the read-only observation session")
            from nautilus_trader.model import Venue

            from lab.paper import IB

            account = self.cache.account_for_venue(Venue(IB))  # IBKR routes every listing venue to one account
            aid = self.cache.account_id(Venue(IB))
            balances = []
            if account is not None:
                for b in account.balances().values():
                    balances.append(
                        {
                            "currency": str(b.currency),
                            # Nautilus Money.__str__ may include the currency suffix.
                            # B01 requires exact numeric strings with currency in its own field.
                            "total": str(b.total.as_decimal()),
                            "free": str(b.free.as_decimal()),
                            "locked": str(b.locked.as_decimal()),
                        }
                    )
            positions = {}
            for t, iid in iids.items():
                try:
                    open_ = self.cache.positions_open(instrument_id=iid)
                    positions[t] = {
                        "net": sum(float(x.signed_qty) for x in open_),
                        # Native Quantity serializes exactly; use the float signed_qty only for direction.
                        # Independent reconciliation must never interpret the rounded net above as exact.
                        "net_exact": str(
                            sum(
                                (
                                    Decimal(str(x.quantity)) * (Decimal("-1") if x.signed_qty < 0 else Decimal("1"))
                                    for x in open_
                                ),
                                Decimal("0"),
                            )
                        ),
                        "positions": [
                            {"side": str(x.side), "quantity": str(x.quantity), "avg_px_open": str(x.avg_px_open)}
                            for x in open_
                        ],
                    }
                except Exception as exc:  # evidence, not control flow
                    positions[t] = f"unavailable: {type(exc).__name__}: {exc}"
            orders = [_order_record(o) for o in self.cache.orders()]
            _write(out_dir / "broker_account.json", {"account_id": str(aid) if aid else None, "balances": balances})
            _write(out_dir / "broker_positions.json", positions)
            _write(out_dir / "broker_orders.json", orders)
            _write(out_dir / "broker_bars.json", self.bars)
            _write(
                out_dir / "broker_capture_meta.json",
                {
                    "capture_started_ns": capture_started_ns,
                    "capture_completed_ns": time.time_ns(),
                    "stream": "NAUTILUS_IBKR_READ_ONLY",
                    "coverage_verified": False,
                    "broker_authentication_verified": False,
                    "causal_boundary_verified": False,
                },
            )
            self.shutdown_system("capture snapshot written")

    return CaptureActor()


def snapshot(
    cfg: LabConfig,
    tickers: list[str],
    out: Path,
    phase: str,
    *,
    wait_s: float = 45.0,
    history_days: int = 14,
    clients=None,
    specs=(),
) -> Path:
    """Connect read-only, record the broker's view, return the phase directory. `clients`: the harness's
    simulated broker in tests (paper.Clients); default Interactive Brokers."""
    from lab.paper import _base_node, acquire_runtime_lock
    from lab.safety import check_paper_settings

    # Fail before creating a misleading partial snapshot/identity directory.
    # This is a read-only collection tool, but it must require a configured PAPER account.
    check_paper_settings(cfg.paper.port, cfg.paper.account_id)
    out = check_outside_repo(cfg, out)
    d = phase_dir(out, phase) / f"snapshot-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    d.mkdir(parents=True, mode=0o700)
    _set_capture_status(d, "IN_PROGRESS")
    try:
        _write(d / "identity.json", identity(cfg, specs))
        lock = acquire_runtime_lock(cfg)  # the paper node must be stopped: one connection, no interference
        try:
            node, iids = _base_node(cfg, tickers, clients)
            node.add_actor(_make_actor(cfg, iids, d, wait_s, history_days))
            try:
                node.run()
            finally:
                node.dispose()
        finally:
            lock.close()
        _verify_capture_files(
            d, expected_account=cfg.paper.account_id, expected_port=cfg.paper.port, tickers=tickers
        )
    except BaseException:
        # Preserve private incident evidence but never mistake this folder for a completed B01 capture.
        _set_capture_status(d, "INCOMPLETE")
        raise
    # Status is the final atomic commit marker; any earlier failure leaves the
    # capture INCOMPLETE or IN_PROGRESS, never eligible for inspection.
    _write(d / "NOTE.json", {"note": GRADING_NOTE, "files": sorted(p.name for p in d.iterdir())})
    _set_capture_status(d, "COMPLETE_UNVERIFIED")
    return d


async def snapshot_async(cfg, tickers, out, phase, *, wait_s=3.0, history_days=14, clients=None, specs=()):
    """The same, inside a running event loop (the harness)."""
    import asyncio

    from lab.paper import _base_node, acquire_runtime_lock

    out = check_outside_repo(cfg, out)
    d = phase_dir(out, phase) / f"snapshot-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    d.mkdir(parents=True, mode=0o700)
    _set_capture_status(d, "IN_PROGRESS")
    try:
        _write(d / "identity.json", identity(cfg, specs))
        lock = acquire_runtime_lock(cfg)
        try:
            node, iids = _base_node(cfg, tickers, clients)
            node.add_actor(_make_actor(cfg, iids, d, wait_s, history_days))
            task = asyncio.ensure_future(node.run_async())
            try:
                await asyncio.wait_for(task, wait_s + 30)
            finally:
                if not task.done():
                    node.handle().stop()
                    await asyncio.wait_for(task, 30)
                node.dispose()
        finally:
            lock.close()
        _verify_capture_files(
            d, expected_account=cfg.paper.account_id, expected_port=cfg.paper.port, tickers=tickers
        )
    except BaseException:
        _set_capture_status(d, "INCOMPLETE")
        raise
    # Status is the final atomic commit marker; any earlier failure leaves the
    # capture INCOMPLETE or IN_PROGRESS, never eligible for inspection.
    _write(d / "NOTE.json", {"note": GRADING_NOTE, "files": sorted(p.name for p in d.iterdir())})
    _set_capture_status(d, "COMPLETE_UNVERIFIED")
    return d


# ----------------------------------------------------------------------------- bundle (the lab's own records)

_RUNTIME_PATTERNS = (
    "journal-*.jsonl",
    "ledger*",
    "risk.json",
    "heartbeat.json",
    "HALT",
    "KILL",
    "recoveries.jsonl",
    "smoke-*.json",
    "identity.json",
    "authority.json",
    "notifications*",
)
_NEVER_COPY = ("authority.key",)  # a private signing key is not evidence


def _sqlite_row(table: str, row: dict) -> dict:
    """One exported row. TEXT that holds JSON is parsed, so the sanitizer can reach nested IDs; the execution
    store's history rows are labelled by what their key is (a trade ID, or a client order ID)."""
    parsed = {}
    for k, v in row.items():
        if isinstance(v, str) and v[:1] in ("[", "{"):
            with contextlib.suppress(json.JSONDecodeError):
                v = json.loads(v)
        parsed[k] = v
    if table == "history" and {"bucket", "id", "value"} <= set(parsed):
        if parsed["bucket"] == "applied":
            ts, coid = (parsed["value"] + [None, None])[:2] if isinstance(parsed["value"], list) else (None, None)
            return {"bucket": "applied", "trade_id": parsed["id"], "client_order_id": coid, "applied_ns": ts}
        return {"bucket": parsed["bucket"], "client_order_id": parsed["id"], "intent": parsed["value"]}
    return parsed


def sqlite_export(path: Path) -> dict:
    """Deterministic, read-only export of every table: schema, row counts and rows in primary-key order, with
    the database's version and integrity check. Run it on a consistent copy (bundle makes one)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {
            "file": path.name,
            "sha256": _sha256(path),
            "user_version": con.execute("PRAGMA user_version").fetchone()[0],
            "integrity": con.execute("PRAGMA quick_check").fetchone()[0],
            "tables": {},
        }
        for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
            order = ", ".join(f'"{c}"' for c in cols)
            rows = con.execute(f'SELECT * FROM "{name}" ORDER BY {order}').fetchall()
            out["tables"][name] = {
                "schema": sql,
                "row_count": len(rows),
                "rows": [_sqlite_row(name, dict(zip(cols, r, strict=True))) for r in rows],
            }
        return out
    finally:
        con.close()


def bundle(cfg: LabConfig, out: Path, phase: str) -> Path:
    """Copy the lab's runtime records for a phase. Takes the runtime lock, so `lab paper` must be stopped (a
    cold copy: ChatGPT, C004-F3); SQLite databases are copied with SQLite's backup API and exported as
    deterministic JSON next to the copy (C004-F1), so `sanitize` can produce a reviewable version."""
    from lab.paper import acquire_runtime_lock, runtime_dir

    out = check_outside_repo(cfg, out)
    lock = acquire_runtime_lock(cfg)
    try:
        d = phase_dir(out, phase) / f"runtime-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
        d.mkdir(parents=True, mode=0o700)
        rt = runtime_dir(cfg)
        copied = []
        for src in sorted({p for pattern in _RUNTIME_PATTERNS for p in rt.glob(pattern)} | set(rt.glob("*.sqlite3"))):
            if not src.is_file() or src.name in _NEVER_COPY or src.name.endswith((".tmp", "-journal", "-wal", "-shm")):
                continue
            if src.suffix == ".sqlite3":
                source, copy = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(d / src.name)
                try:
                    source.backup(copy)  # a consistent snapshot, whatever the journal mode
                finally:
                    source.close()
                    copy.close()
                _write(d / f"{src.name}.export.json", sqlite_export(d / src.name))
                copied += [src.name, f"{src.name}.export.json"]
            else:
                shutil.copy2(src, d / src.name)
                copied.append(src.name)
        _write(d / "identity.json", identity(cfg))
        _write(d / "NOTE.json", {"note": GRADING_NOTE, "files": copied})
        return d
    finally:
        lock.close()


# ----------------------------------------------------------------------------- sanitize (for the repository)

_ACCOUNT = re.compile(r"\b(?:IB-)?[DU][A-Z]?\d{5,9}\b")  # DU1234567, U1234567, IB-DU1234567
_SECRET_KEYS = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|hmac|chat_id|userid)")
_ID_KEYS = ("client_order_id", "venue_order_id", "trade_id", "order_id", "account_id", "account", "coid")
_KEYED_BY_ORDER = ("pending", "terminal", "disposed")  # maps whose keys are client order IDs


def _key(path: Path) -> bytes:
    path = path.expanduser()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    return path.read_bytes()


def _pseudo(key: bytes, kind: str, value: str) -> str:
    return f"{kind}-" + hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:12]


def _collect_ids(key: bytes, value, ids: dict[str, str], field: str | None = None) -> None:
    """Discover typed IDs first, so a short broker ID in another file's free text is also scrubbed."""
    if isinstance(value, dict):
        if field in _KEYED_BY_ORDER:
            for raw, nested in value.items():
                _record_id(ids, str(raw), _pseudo(key, "CLIENT", str(raw)))
                _collect_ids(key, nested, ids)
            return
        for k, v in value.items():
            if not _SECRET_KEYS.search(str(k)):
                _collect_ids(key, v, ids, str(k))
    elif isinstance(value, list):
        for item in value:
            _collect_ids(key, item, ids, field)
    elif isinstance(value, str) and field in _ID_KEYS and value and value != "None":
        kind = "ACCT" if field.startswith("account") else field.split("_")[0].upper()
        _record_id(ids, value, _pseudo(key, kind, value.removeprefix("IB-") if kind == "ACCT" else value))


def _record_id(ids: dict[str, str], raw: str, replacement: str) -> None:
    previous = ids.get(raw)
    if previous is None or previous == replacement:
        ids[raw] = replacement
        return
    # Some brokers use the identical ID for their client and venue order ID.
    # Keep the CLIENT pseudonym consistently wherever it is referenced.
    if previous.startswith("CLIENT-") and replacement.startswith(("VENUE-", "ORDER-", "COID-")):
        return
    if replacement.startswith("CLIENT-") and previous.startswith(("VENUE-", "ORDER-", "COID-")):
        ids[raw] = replacement
        return
    raise ValueError("ambiguous raw identifier assigned conflicting pseudonym kinds")


def _redact_text(key: bytes, text: str, ids: dict[str, str] | None = None) -> str:
    result = _ACCOUNT.sub(lambda m: _pseudo(key, "ACCT", m.group(0).removeprefix("IB-")), text)
    if ids:
        # One substitution pass prevents replacing substrings of previously emitted pseudonyms.
        pattern = re.compile(
            r"(?<![A-Za-z0-9])(?:"
            + "|".join(re.escape(raw) for raw in sorted(ids, key=len, reverse=True))
            + r")(?![A-Za-z0-9])"
        )
        result = pattern.sub(lambda match: ids[match.group(0)], result)
    return result


def _redact(key: bytes, value, field: str | None = None, ids: dict[str, str] | None = None):
    if isinstance(value, dict) and field in _KEYED_BY_ORDER:
        return {
            (ids.get(str(k), _pseudo(key, "CLIENT", str(k))) if ids else _pseudo(key, "CLIENT", str(k))): _redact(
                key, v, ids=ids
            )
            for k, v in value.items()
        }
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "[REDACTED]"
            else:
                out[_redact_text(key, str(k), ids)] = _redact(key, v, str(k), ids)
        return out
    if isinstance(value, list):
        return [_redact(key, v, field, ids) for v in value]
    if isinstance(value, str):
        if field in _ID_KEYS and value and value != "None":
            kind = "ACCT" if field.startswith("account") else field.split("_")[0].upper()
            return (
                ids.get(value, _pseudo(key, kind, value.removeprefix("IB-") if kind == "ACCT" else value))
                if ids
                else _pseudo(key, kind, value.removeprefix("IB-") if kind == "ACCT" else value)
            )
        return _redact_text(key, value, ids)
    return value


def sanitize(src: Path, dst: Path, key_path: Path) -> dict:
    """Redact every capture file under src into dst. Returns the manifest (also written to dst)."""
    src, dst = src.expanduser().resolve(), dst.expanduser().resolve()
    if dst == src or src in dst.parents:
        raise ValueError("write the sanitized copy outside the raw capture directory")
    key = _key(key_path)
    files = sorted(p for p in src.rglob("*") if p.is_file())
    ids: dict[str, str] = {}
    # Evidence references broker IDs across journal, SQLite export and notes. A single
    # record's typed ID is not enough: scrub its free-text copies in every file.
    for source in files:
        if source.name in _NEVER_COPY or (
            source.suffix not in (".json", ".jsonl") and source.name not in ("HALT", "KILL")
        ):
            continue
        try:
            raw_text = source.read_text()
            if source.suffix == ".jsonl":
                for line in raw_text.splitlines():
                    if line.strip():
                        _collect_ids(key, json.loads(line), ids)
            else:
                _collect_ids(key, json.loads(raw_text), ids)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue  # unparseable files will not be copied in the output pass
    entries = []
    for f in files:
        rel = f.relative_to(src)
        if f.name in _NEVER_COPY:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = f.read_bytes()
        try:
            if f.suffix == ".jsonl":
                lines = [
                    json.dumps(_redact(key, json.loads(line), ids=ids), sort_keys=True)
                    for line in raw.decode().splitlines()
                    if line.strip()
                ]
                text = "\n".join(lines) + ("\n" if lines else "")
            elif f.suffix == ".json" or f.name in ("HALT", "KILL"):
                text = json.dumps(_redact(key, json.loads(raw), ids=ids), indent=1, sort_keys=True)
            else:
                text = None
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = None
        if text is None:
            entries.append(
                {
                    "path": str(rel),
                    "original_sha256": hashlib.sha256(raw).hexdigest(),
                    "redacted": None,
                    "note": "binary or non-JSON: not copied (review the original)",
                }
            )
            continue
        target.write_text(text)
        entries.append(
            {"path": str(rel), "original_sha256": hashlib.sha256(raw).hexdigest(), "redacted_sha256": _sha256(target)}
        )
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "procedure": (
            "lab.capture.sanitize: account IDs (DU…/U…, IB-…) and order/trade/account ID fields pseudonymized "
            "with HMAC-SHA256 under a private key kept outside Git (same key for every phase, so joins hold); "
            "fields named like password/token/secret/api key/hmac/chat id/user id replaced by [REDACTED]; "
            "authority.key never copied; SQLite databases are represented by their deterministic JSON export; "
            "non-JSON files (raw databases included) listed with their hash but not copied. Free text (operator "
            "reasons, broker messages) has account and known typed ID patterns removed, but remains "
            "unverified for other private information and MUST be manually reviewed before publication"
        ),
        "key_fingerprint": hashlib.sha256(key).hexdigest()[:16],
        # ChatGPT, C004-F2: a person reads every sanitized file before publication and records it here; an empty
        # attestation means the bundle is not publishable yet.
        "manual_review_required": True,
        "reviewer_attestation": None,
        "note": GRADING_NOTE,
        "files": entries,
    }
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return manifest
