"""Portable Binance USD-M market-event capture using the durable JSONL journal.

Runs without NautilusTrader. Public market data only: no credentials or orders.
A separate journal per WebSocket endpoint provides bounded in-memory buffering
(the websocket library's max_queue), and status events make reconnects visible.
No guarantee of completeness: reconnects, source sampling, and process downtime
remain gaps requiring explicit treatment in any downstream study.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import re
import shutil
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from lab.market_journal import JournalWriter, recover_directory

HOST = "wss://fstream.binance.com"
SYMBOL_RE = re.compile(r"[A-Z0-9]{2,20}\Z")


class LowDiskSpace(RuntimeError):
    """The reserved free-space budget has been exhausted: stop writing."""


class DiskSpaceGuard:
    """Amortized free-disk check, shared by both feed tasks.

    The minimum is enforced at startup and periodically during capture.
    A successful check cannot prevent another process from consuming the
    reserve or protect against a sudden disk I/O failure.
    """

    def __init__(
        self,
        directory: Path,
        min_free_mib: int = 512,
        *,
        interval_s: float = 2.0,
        clock=time.monotonic,
        free_bytes=None,
    ) -> None:
        if min_free_mib < 64:
            raise ValueError("min_free_mib must be at least 64 MiB")
        if interval_s <= 0:
            raise ValueError("disk check interval must be positive")
        self.directory = Path(directory)
        self.minimum_bytes = min_free_mib * 1024 * 1024
        self.interval_s = interval_s
        self.clock = clock
        self.free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.next_check = 0.0

    def check(self, *, force: bool = False) -> None:
        now = self.clock()
        if not force and now < self.next_check:
            return
        available = self.free_bytes(self.directory)
        if available < self.minimum_bytes:
            raise LowDiskSpace(
                f"recording stopped: {available / 1048576:.1f} MiB free in "
                f"{self.directory}, below {self.minimum_bytes / 1048576:.0f} MiB reserve"
            )
        self.next_check = now + self.interval_s


def stream_urls(symbols: list[str]) -> dict[str, str]:
    """Match Binance's separate public (quotes) and market (trades) endpoints."""
    if not symbols or len(set(symbols)) != len(symbols) or any(not SYMBOL_RE.fullmatch(s) for s in symbols):
        raise ValueError("symbols must be unique uppercase Binance symbols (e.g. BTCUSDT)")
    low = [symbol.lower() for symbol in symbols]
    public = "/".join(f"{s}@bookTicker" for s in low)
    market = "/".join(
        channel
        for s in low
        for channel in (f"{s}@aggTrade", f"{s}@markPrice@1s", f"{s}@forceOrder")
    )
    return {
        "public": f"{HOST}/public/stream?streams={public}",
        "market": f"{HOST}/market/stream?streams={market}",
    }


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"nonfinite JSON value: {value}")


def _retryable_transport_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, ConnectionError, TimeoutError, EOFError)):
        return True
    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        return False
    return isinstance(exc, ConnectionClosed)


def parse_message(raw: str | bytes) -> tuple[str, dict] | None:
    """Return a validated combined-stream event; ignore subscription/control replies."""
    item = json.loads(raw, parse_constant=_reject_nonfinite)
    if not isinstance(item, dict):
        raise ValueError("WebSocket message must be an object")
    stream = item.get("stream")
    if stream is None:
        return None
    data = item.get("data")
    if not isinstance(stream, str) or not stream or not isinstance(data, dict):
        raise ValueError("invalid combined-stream payload")
    return stream, data


async def capture_feed(
    category: str,
    url: str,
    directory: Path,
    session: str,
    deadline: float,
    *,
    connector,
    clock=time.monotonic,
    timestamp=time.time_ns,
    sleep=asyncio.sleep,
    max_reconnect_delay: float = 10.0,
    fault_disconnect_after: int | None = None,
    disk_guard: DiskSpaceGuard | None = None,
) -> dict:
    """Capture one endpoint. Injectable transport makes failure tests network-free.

    Every disconnection is recorded in the journal. There is intentionally no
    unbounded application queue: slow persistence backpressures the WebSocket.
    """
    if fault_disconnect_after is not None and fault_disconnect_after < 1:
        raise ValueError("fault_disconnect_after must be positive")
    counts: Counter[str] = Counter()
    fault_injected = False
    reconnects = 0
    failures = 0
    errors = 0
    writer = JournalWriter(directory, category, session)

    def status(event: str, **fields) -> None:
        writer.append(
            {"received_ns": timestamp(), "stream": "collector.status",
             "data": {"category": category, "event": event, **fields}}
        )

    try:
        while clock() < deadline:
            if disk_guard is not None:
                disk_guard.check()
            try:
                async with connector(url) as websocket:
                    status("connected")
                    failures = 0
                    while clock() < deadline:
                        if disk_guard is not None:
                            disk_guard.check()
                        try:
                            raw = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=min(1.0, max(0.001, deadline - clock())),
                            )
                        except TimeoutError:
                            continue

                        received = timestamp()
                        try:
                            payload = parse_message(raw)
                        except (ValueError, TypeError, UnicodeDecodeError) as exc:
                            errors += 1
                            status("invalid_message", error=type(exc).__name__)
                            continue
                        if payload is None:
                            status("control_message")
                            continue
                        stream, data = payload
                        if disk_guard is not None:
                            disk_guard.check()
                        writer.append({"received_ns": received, "stream": stream, "data": data})
                        counts[stream] += 1
                        if (
                            fault_disconnect_after is not None
                            and not fault_injected
                            and sum(counts.values()) >= fault_disconnect_after
                        ):
                            fault_injected = True
                            status("fault_injected_disconnect", after_events=fault_disconnect_after)
                            await websocket.close()

            except Exception as exc:
                if not _retryable_transport_error(exc):
                    raise
                reconnects += 1
                failures += 1
                status("disconnected", reason=type(exc).__name__, reconnects=reconnects)
                if clock() >= deadline:
                    break
                await sleep(min(max_reconnect_delay, 2 ** min(failures, 8)))
            else:
                if clock() < deadline:
                    reconnects += 1
                    failures += 1
                    status("disconnected", reason="closed", reconnects=reconnects)
                    await sleep(min(max_reconnect_delay, 2 ** min(failures, 8)))
    finally:
        writer.close()

    return {"category": category, "counts": dict(counts), "reconnects": reconnects, "invalid": errors}


async def run_capture(
    directory: Path,
    symbols: list[str],
    duration: float,
    *,
    connector,
    fault_disconnect_after: int | None = None,
    min_free_mib: int = 512,
    free_bytes=None,
) -> list[dict]:
    """Exclusive single-process capture: recover only after acquiring the lock."""
    if duration <= 0:
        raise ValueError("duration must be positive")
    if fault_disconnect_after is not None and fault_disconnect_after < 1:
        raise ValueError("fault_disconnect_after must be positive")
    urls = stream_urls(symbols)
    if min_free_mib < 64:
        raise ValueError("min_free_mib must be at least 64 MiB")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".capture.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another market capture owns {directory}") from exc
        try:
            disk_guard = DiskSpaceGuard(directory, min_free_mib, free_bytes=free_bytes)
            disk_guard.check(force=True)
            restored = recover_directory(directory)
            for report in restored:
                print(
                    f"Recovered {report.path.name}: {report.records} records, "
                    f"{report.discarded_bytes} trailing bytes discarded",
                    flush=True,
                )
            session = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            deadline = time.monotonic() + duration
            return await asyncio.gather(
                *(
                    capture_feed(
                        category, url, directory, session, deadline, connector=connector,
                        fault_disconnect_after=fault_disconnect_after,
                        disk_guard=disk_guard,
                    )
                    for category, url in urls.items()
                )
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-only public Binance market-data journal capture")
    parser.add_argument("--out", type=Path, default=Path("data/market-journal"))
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument(
        "--min-free-mib", type=int, default=512,
        help="fail-closed free-space reserve in MiB (minimum 64; default 512)",
    )
    parser.add_argument(
        "--inject-disconnect-after", type=int, default=None, metavar="N",
        help="qualification only: close each feed once after N market events to test reconnection",
    )
    args = parser.parse_args(argv)

    # Imported only when running live; unit tests and recovery need no WebSocket package.
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        parser.error("install websockets>=14,<17 to capture exchange data")
        raise AssertionError("unreachable") from exc

    def connector(url: str):
        return connect(
            url,
            open_timeout=15,
            max_queue=16,
            ping_interval=20,
            ping_timeout=20,
        )

    try:
        summary = asyncio.run(
            run_capture(
                args.out, args.symbols, args.seconds, connector=connector,
                fault_disconnect_after=args.inject_disconnect_after,
                min_free_mib=args.min_free_mib,
            )
        )
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    for result in summary:
        print(
            f"{result['category']}: {sum(result['counts'].values())} events, "
            f"{result['reconnects']} reconnects, {result['invalid']} invalid messages",
            flush=True,
        )
        for stream, count in sorted(result["counts"].items()):
            print(f"  {stream}: {count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
