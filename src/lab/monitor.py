"""
Read-only monitoring for the market-data recorder: is it alive, is every feed flowing, is the data reaching the
catalog, how much has been recorded and how long the disk lasts at this rate.

`lab monitor` serves it as a local dashboard (GET /) and as JSON (GET /api/status; GET /health answers 503 while
no recorder is running). It reads <out>/heartbeats/<venue>.json and the files under <out>; it never writes there
and never talks to an exchange, so it can run beside the recorders, or after them, on the same data directory.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lab.persistence import read_json
from lab.safety import read_kill_switch

NS = 1_000_000_000
HEARTBEAT_MAX_AGE_S = 120  # the recorder writes every 10 s; the compose healthcheck allows the same 120 s
PROMOTE_EVERY_S = 3600  # lab.record promotes the stream into the catalog every hour
PROMOTE_GRACE_S = 900
# Time series in the catalog. Everything else there is an instrument definition (e.g. crypto_perpetual), stamped
# with the time it was loaded, which is no event time.
SERIES = {
    "order_book_deltas",
    "order_book_depths",
    "quotes",
    "trades",
    "bars",
    "mark_prices",
    "index_prices",
    "funding_rates",
}
GAP_S = 60  # a hole longer than this between consecutive order-book files is an outage
DISK_WARN_DAYS = 7
DISK_FAIL_DAYS = 1
HISTORY_S = 1800  # event-rate history kept for the dashboard
RATE_WINDOW_S = 60
CATALOG_TTL_S = 30  # the catalog only changes at promotion; rescanning it on every request is waste
PAPER_MAX_AGE_S = 30  # `lab fast paper` rewrites its report every 5 s
DAY_LOSS_WARN = 0.8  # share of the daily loss cap used
RISK_EVENTS = 20  # latest risk-audit entries shown

# "2025-10-20T22-40-00-001000000Z_2025-10-20T22-40-00-049000000Z_63736a17c5458205.parquet"
_FILE = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)-(\d{9})Z_(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)-(\d{9})Z")


def _ns(day: str, h: str, m: str, s: str, frac: str) -> int:
    whole = datetime.fromisoformat(f"{day}T{h}:{m}:{s}+00:00")
    return int(whole.timestamp()) * NS + int(frac)


def file_span(name: str) -> tuple[int, int] | None:
    """(first, last) event time in ns from a catalog file name, or None for a name the catalog didn't write."""
    m = _FILE.match(name)
    return (_ns(*m.groups()[:5]), _ns(*m.groups()[5:])) if m else None


def _iso(ns: int | None) -> str | None:
    return datetime.fromtimestamp(ns / NS, UTC).isoformat(timespec="seconds") if ns else None


def _parse_iso(text) -> int | None:
    try:
        return int(datetime.fromisoformat(text).timestamp() * NS)
    except (TypeError, ValueError):
        return None


def gaps(spans: list[tuple[int, int]], min_gap_ns: int) -> list[tuple[int, int]]:
    """Holes longer than min_gap_ns between time spans (overlaps allowed), oldest first."""
    out, covered = [], None
    for first, last in sorted(spans):
        if covered is not None and first - covered > min_gap_ns:
            out.append((covered, first))
        covered = last if covered is None else max(covered, last)
    return out


class Catalog:
    """Inventory of a NautilusTrader Parquet catalog: files, bytes, rows and time coverage per type and
    instrument. Row counts come from each file's Parquet footer and are cached by (size, mtime)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._rows: dict[Path, tuple[int, int, int]] = {}

    def _count(self, path: Path, stat) -> int:
        key = (stat.st_size, stat.st_mtime_ns)
        hit = self._rows.get(path)
        if hit and hit[:2] == key:
            return hit[2]
        import pyarrow.parquet as pq

        try:
            rows = pq.ParquetFile(path).metadata.num_rows
        except Exception:  # a file being replaced right now; counted on the next scan
            rows = 0
        self._rows[path] = (*key, rows)
        return rows

    def scan(self) -> dict:
        """{type: {instrument: {files, bytes, rows, spans}}}"""
        data, out, seen = self.root / "data", {}, set()
        if not data.is_dir():
            return out
        for kind in sorted(p for p in data.iterdir() if p.is_dir()):
            for inst in sorted(p for p in kind.iterdir() if p.is_dir()):
                entry = {"files": 0, "bytes": 0, "rows": 0, "spans": []}
                for f in inst.glob("*.parquet"):
                    try:
                        stat = f.stat()
                    except FileNotFoundError:
                        continue
                    seen.add(f)
                    entry["files"] += 1
                    entry["bytes"] += stat.st_size
                    entry["rows"] += self._count(f, stat)
                    if span := file_span(f.name):
                        entry["spans"].append(span)
                out.setdefault(kind.name, {})[inst.name] = entry
        self._rows = {p: v for p, v in self._rows.items() if p in seen}
        return out


def _stream(out: Path, now_ns: int, started: dict[str, int | None]) -> dict:
    """Files waiting in out/stream/<venue> to be promoted into the catalog. A file last written before its venue's
    recorder started is left over from an earlier run that stopped without saving (killed, or a stop that timed
    out): a recorder only ever promotes its own run's files, so these stay out of the catalog for good."""
    files, size, oldest, left_files, left_size = 0, 0, None, 0, 0
    stream = out / "stream"
    if stream.is_dir():
        for f in stream.rglob("*"):
            try:
                if not f.is_file() or f.name.startswith("_nautilus"):
                    continue
                stat = f.stat()
            except FileNotFoundError:
                continue
            files += 1
            size += stat.st_size
            oldest = stat.st_mtime_ns if oldest is None else min(oldest, stat.st_mtime_ns)
            since = started.get(f.relative_to(stream).parts[0].upper())
            if since is None or stat.st_mtime_ns < since:
                left_files += 1
                left_size += stat.st_size
    return {
        "files": files,
        "bytes": size,
        "oldest_age_s": (now_ns - oldest) / NS if oldest else None,
        "leftover": {"files": left_files, "bytes": left_size},
    }


class Rates:
    """Events per second per instrument, from successive heartbeats. A recorder restart resets its counters,
    which clears the history instead of showing a negative rate."""

    def __init__(self, keep_s: int = HISTORY_S):
        self.keep_ns = keep_s * NS
        self.samples: deque[tuple[int, dict[str, int]]] = deque()

    def add(self, at_ns: int, totals: dict[str, int]):
        if self.samples and at_ns <= self.samples[-1][0]:
            return
        if self.samples and any(totals.get(k, 0) < v for k, v in self.samples[-1][1].items()):
            self.samples.clear()
        self.samples.append((at_ns, totals))
        while self.samples and at_ns - self.samples[0][0] > self.keep_ns:
            self.samples.popleft()

    def current(self, window_s: int = RATE_WINDOW_S) -> dict[str, float]:
        if len(self.samples) < 2:
            return {}
        last_at, last = self.samples[-1]
        first_at, first = next(
            ((t, c) for t, c in self.samples if last_at - t <= window_s * NS and t < last_at), self.samples[-2]
        )
        dt = (last_at - first_at) / NS
        return {k: (v - first.get(k, 0)) / dt for k, v in last.items()}

    def history(self) -> dict[str, list[list]]:
        """{instrument: [[iso time, events/s], ...]} between consecutive samples."""
        out: dict[str, list[list]] = {}
        pairs = list(zip(self.samples, list(self.samples)[1:], strict=False))
        for (t0, c0), (t1, c1) in pairs:
            dt = (t1 - t0) / NS
            for k, v in c1.items():
                out.setdefault(k, []).append([_iso(t1), round((v - c0.get(k, 0)) / dt, 3)])
        return out


def _tail_jsonl(path: Path, n: int, max_bytes: int = 65536) -> list[dict]:
    """The last n JSON lines of an append-only log, newest first, with ts_ns as an ISO time."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            lines = f.read().decode(errors="replace").splitlines()[1 if size > max_bytes else 0 :]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            if isinstance(entry.get("ts_ns"), int):
                entry["at"] = _iso(entry.pop("ts_ns"))
            out.append(entry)
        if len(out) == n:
            break
    return out


def _check(name: str, state: str, detail: str) -> dict:
    return {"name": name, "state": state, "detail": detail}


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds / size:.1f} {unit}"
    return f"{seconds} s"


class Monitor:
    """Builds the status of one recorder output directory (lab.record's <out>), where one recorder per venue
    writes <out>/heartbeats/<venue>.json and all of them share <out>/catalog."""

    def __init__(self, out: Path, *, results: Path | None = None, catalog_ttl_s: int = CATALOG_TTL_S):
        """results: the lab's results directory, for the paper-trading report, the risk audit and the kill
        switch (None: recorders only)."""
        self.out = Path(out)
        self.results = Path(results) if results else None
        self.catalog = Catalog(self.out / "catalog")
        self.rates: dict[str, Rates] = {}
        self.ttl_ns = catalog_ttl_s * NS
        self._scan: tuple[int, dict] | None = None
        self.lock = threading.Lock()

    def _heartbeats(self) -> dict[str, dict | None]:
        """{venue: heartbeat, or None when unreadable}"""
        out = {}
        for f in sorted((self.out / "heartbeats").glob("*.json")):
            try:
                hb = read_json(f)
            except (OSError, ValueError):
                hb = None
            out[f.stem.upper()] = hb if isinstance(hb, dict) and isinstance(hb.get("instruments"), dict) else None
        return out

    def _inventory(self, now_ns: int) -> dict:
        if self._scan is None or now_ns - self._scan[0] > self.ttl_ns or now_ns < self._scan[0]:
            self._scan = (now_ns, self.catalog.scan())
        return self._scan[1]

    def status(self, now_ns: int | None = None) -> dict:
        with self.lock:
            return self._status(time.time_ns() if now_ns is None else now_ns)

    def _recorder(self, venue: str, hb: dict | None, inv: dict, now_ns: int) -> tuple[dict, list[dict]]:
        """One venue's recorder summary and its checks."""
        hb_at = _parse_iso(hb.get("at")) if hb else None
        started = _parse_iso(hb.get("started")) if hb else None
        hb_age = (now_ns - hb_at) / NS if hb_at else None
        running = bool(hb and hb.get("running")) and hb_age is not None and hb_age <= HEARTBEAT_MAX_AGE_S
        feeds = hb["instruments"] if hb else {}
        rates = self.rates.setdefault(venue, Rates())
        if hb_at:
            rates.add(hb_at, {k: sum(v for v in c.values() if isinstance(v, int)) for k, c in feeds.items()})
        rate = rates.current() if running else {}
        name = venue.lower()

        checks = []
        if not hb:
            checks.append(_check(name, "fail", "heartbeat unreadable"))
        elif not hb.get("running"):
            checks.append(_check(name, "fail", f"recorder stopped at {hb.get('at')}"))
        elif not running:
            checks.append(_check(name, "fail", f"last heartbeat {_age(hb_age or 0)} ago: recorder not running"))
        else:
            silent = sorted(i for i in feeds if i in set(hb.get("stale", [])))
            up = f"up {_age((now_ns - started) / NS)}" if started else "running"
            checks.append(
                _check(name, "warn", "no data for over a minute: " + ", ".join(silent))
                if silent
                else _check(name, "ok", f"{up}, {len(feeds)} instrument(s) live")
            )
            due = PROMOTE_EVERY_S + PROMOTE_GRACE_S
            newest = max(
                (s[1] for kind, by in inv.items() if kind in SERIES for i in feeds if i in by for s in by[i]["spans"]),
                default=None,
            )
            if started and (now_ns - started) / NS > due and (newest is None or (now_ns - newest) / NS > due):
                since = f"since {_iso(newest)}" if newest else "since the recorder started"
                checks.append(_check(f"{name} catalog", "fail", f"nothing promoted into the catalog {since}"))
        summary = {
            "venue": venue,
            "running": running,
            "heartbeat_at": _iso(hb_at),
            "heartbeat_age_s": hb_age,
            "started": _iso(started),
            "uptime_s": (now_ns - started) / NS if running and started else None,
            "events_per_s": sum(rate.values()) if rate else None,
            "feeds": feeds,
            "stale": set(hb.get("stale", [])) if hb else set(),
            "rate": rate,
            "history": rates.history(),
        }
        return summary, checks

    def _status(self, now_ns: int) -> dict:
        inv = self._inventory(now_ns)
        recorders, checks = [], []
        for venue, hb in self._heartbeats().items():
            summary, venue_checks = self._recorder(venue, hb, inv, now_ns)
            recorders.append(summary)
            checks += venue_checks
        if not recorders:
            checks.append(_check("recorder", "fail", f"no heartbeat in {self.out / 'heartbeats'}"))
        by_venue = {r["venue"]: r for r in recorders}

        stream = _stream(self.out, now_ns, {r["venue"]: _parse_iso(r["started"]) for r in recorders})
        if left := stream["leftover"]["files"]:
            checks.append(
                _check(
                    "stream",
                    "warn",
                    f"{stream['leftover']['bytes'] / 1e6:.1f} MB in {left} file(s) left by a recorder run that stopped "
                    "without saving; they are never promoted into the catalog",
                )
            )

        # Recorded instruments: the live feeds, plus any with market data in the catalog. Not every instrument
        # definition in it: an adapter that loads a whole venue (Bybit) streams hundreds of definitions.
        instruments = []
        feed_ids = {i for r in recorders for i in r["feeds"]}
        for iid in sorted(feed_ids | {i for kind, by in inv.items() if kind in SERIES for i in by}):
            venue = iid.rsplit(".", 1)[-1]
            r = by_venue.get(venue)
            feed = r["feeds"].get(iid) if r else None
            last = _parse_iso(feed.get("last_event")) if feed else None
            book = inv.get("order_book_deltas", {}).get(iid)
            spans = sorted(book["spans"]) if book else []
            holes = gaps(spans, GAP_S * NS)
            first, end = (spans[0][0], max(s[1] for s in spans)) if spans else (None, None)
            span_s = (end - first) / NS if spans else 0
            missing_s = sum(b - a for a, b in holes) / NS
            stored = [by[iid] for by in inv.values() if iid in by]
            instruments.append(
                {
                    "id": iid,
                    "symbol": iid.rsplit(".", 1)[0],
                    "venue": venue,
                    "state": (
                        "not recording"
                        if not feed or not r["running"]
                        else "stale"
                        if iid in r["stale"] or not last
                        else "live"
                    ),
                    "last_event": _iso(last),
                    "last_event_age_s": (now_ns - last) / NS if last else None,
                    "counts": {k: v for k, v in feed.items() if isinstance(v, int)} if feed else {},
                    "events_per_s": r["rate"].get(iid) if r and r["running"] else None,
                    "history": r["history"].get(iid, []) if r else [],
                    "catalog": {
                        "files": sum(e["files"] for e in stored),
                        "bytes": sum(e["bytes"] for e in stored),
                        "rows": sum(e["rows"] for e in stored),
                        "first": _iso(first),
                        "last": _iso(end),
                        "coverage_pct": 100 * (1 - missing_s / span_s) if span_s > 0 else None,
                        "gaps": [{"from": _iso(a), "to": _iso(b), "seconds": (b - a) / NS} for a, b in holes][-20:],
                    },
                }
            )

        by_type = {
            kind: {k: sum(e[k] for e in by.values()) for k in ("files", "bytes", "rows")} for kind, by in inv.items()
        }
        total = {k: sum(t[k] for t in by_type.values()) for k in ("files", "bytes", "rows")}
        all_spans = [s for kind, by in inv.items() if kind in SERIES for e in by.values() for s in e["spans"]]
        newest = max((s[1] for s in all_spans), default=None)
        oldest = min((s[0] for s in all_spans), default=None)

        usage = shutil.disk_usage(self.out if self.out.exists() else self.out.parent)
        recorded_s = (newest - oldest) / NS if newest and oldest else 0
        per_day = total["bytes"] / recorded_s * 86400 if recorded_s >= 3600 else None
        runway = usage.free / per_day if per_day else None
        if runway is not None:
            state = "fail" if runway < DISK_FAIL_DAYS else "warn" if runway < DISK_WARN_DAYS else "ok"
            checks.append(_check("disk", state, f"{_age(runway * 86400)} left at {per_day / 1e9:.2f} GB/day"))

        running = [r for r in recorders if r["running"]]
        bad = {c["state"] for c in checks} & {"fail", "warn"}
        overall = "down" if not running else "degraded" if bad else "ok"
        rates = [r["events_per_s"] for r in running if r["events_per_s"] is not None]
        return {
            "now": _iso(now_ns),
            "status": overall,
            "checks": checks,
            "recorders": [
                {k: v for k, v in r.items() if k not in ("feeds", "stale", "rate", "history")} for r in recorders
            ],
            "events_per_s": sum(rates) if rates else None,
            "instruments": instruments,
            "catalog": {**total, "by_type": by_type, "first": _iso(oldest), "last": _iso(newest)},
            "stream": stream,
            "disk": {"free": usage.free, "total": usage.total, "bytes_per_day": per_day, "runway_days": runway},
            "paper": self._paper(now_ns) if self.results else None,
        }

    def _paper(self, now_ns: int) -> dict:
        """`lab fast paper`'s report, the risk manager's latest audit entries and the kill switch. Its status is
        "none" (never run here), "stopped" or "running"; checks flag a stop, a reducing or halted risk manager,
        the kill switch and a day's loss near its cap."""
        fast = self.results / "fast"
        try:
            report = read_json(fast / "paper.json")
            report = report if isinstance(report, dict) else None
        except (OSError, ValueError):
            report = None
        at = _parse_iso(report.get("at")) if report else None
        age = (now_ns - at) / NS if at else None
        running = bool(report and report.get("running")) and age is not None and age <= PAPER_MAX_AGE_S
        kill = read_kill_switch(self.results / "KILL")

        checks = []
        if report:
            checks.append(
                _check("paper", "ok", f"report {_age(age or 0)} ago")
                if running
                else _check("paper", "warn", f"not running; last report {report.get('at')}")
            )
            risk = report.get("risk") or {}
            state = risk.get("state")
            level = {"ACTIVE": "ok", "REDUCING": "warn"}.get(state, "fail")
            checks.append(_check("risk", level, f"{state}{': ' + risk['reason'] if risk.get('reason') else ''}"))
            cap = (risk.get("limits") or {}).get("max_daily_loss")
            loss = -report["day_pnl"] if isinstance(report.get("day_pnl"), int | float) else None
            if cap and loss is not None and loss >= DAY_LOSS_WARN * cap:
                checks.append(_check("daily loss", "warn", f"{loss:.2f} of the {cap:.2f} cap"))
        if kill:
            checks.append(_check("kill switch", "fail", f"{kill['mode']}: {kill.get('reason', '')}"))
        return {
            "status": "running" if running else "stopped" if report else "none",
            "report_age_s": age,
            "report": report,
            "kill": kill,
            "checks": checks,
            "events": _tail_jsonl(fast / "risk-audit.jsonl", RISK_EVENTS),
        }


# ----------------------------------------------------------------------------- HTTP

UI = Path(__file__).parent / "monitor_ui"
# Every path served, fixed: nothing from the request reaches the filesystem.
UI_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/monitor.js": ("monitor.js", "text/javascript"),
    "/monitor.css": ("monitor.css", "text/css"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/fonts/archivo-latin.woff2": ("fonts/archivo-latin.woff2", "font/woff2"),
    "/fonts/archivo-latin-ext.woff2": ("fonts/archivo-latin-ext.woff2", "font/woff2"),
}


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, monitor: Monitor):
        super().__init__(address, Handler)
        self.monitor = monitor
        port = self.server_address[1]
        self.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def reply(self, code, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        # A fixed Host keeps other web pages from reading the status through DNS rebinding.
        if self.headers.get("Host") not in self.server.hosts:
            return self.reply(403, {"error": "local access only"})
        if self.path in ("/api/status", "/health"):
            try:
                s = self.server.monitor.status()
            except Exception as e:  # report, don't drop the connection
                return self.reply(500, {"error": str(e)})
            if self.path == "/health":
                return self.reply(503 if s["status"] == "down" else 200, {"status": s["status"]})
            return self.reply(200, s)
        if self.path not in UI_FILES:
            return self.reply(404, {"error": "not found"})
        name, kind = UI_FILES[self.path]
        return self.reply(200, (UI / name).read_bytes(), kind)


def serve(out: Path, host: str = "127.0.0.1", port: int = 8766, *, results: Path | None = None):
    if host == "0.0.0.0" and not Path("/.dockerenv").exists():
        raise ValueError("listening on all interfaces is reserved for the loopback-published Docker service")
    server = MonitorServer((host, port), Monitor(out, results=results))
    print(f"market-data monitor for {out}: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
