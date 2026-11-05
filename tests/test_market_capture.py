"""Offline regression tests for portable public Binance market-data capture."""

import asyncio
import fcntl
import json
from pathlib import Path

import pytest

from lab.market_capture import (
    DiskSpaceGuard,
    LowDiskSpace,
    capture_feed,
    parse_message,
    run_capture,
    stream_urls,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.2
        return self.value


class FakeSession:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def close(self):
        self.closed = True

    async def recv(self):
        if self.closed:
            raise ConnectionError("intentionally closed")
        try:
            item = next(self.messages)
        except StopIteration:
            raise TimeoutError("no new message") from None
        if isinstance(item, Exception):
            raise item
        return item


def websocket_message(stream="btcusdt@aggTrade", event=1):
    return json.dumps({"stream": stream, "data": {"a": event}})


def test_stream_endpoints_are_separated():
    urls = stream_urls(["BTCUSDT", "ETHUSDT"])
    assert urls["public"].startswith("wss://fstream.binance.com/public/stream?")
    assert "btcusdt@bookTicker" in urls["public"]
    assert "ethusdt@bookTicker" in urls["public"]
    assert "@aggTrade" not in urls["public"]
    assert urls["market"].startswith("wss://fstream.binance.com/market/stream?")
    for suffix in ("@aggTrade", "@markPrice@1s", "@forceOrder"):
        assert "btcusdt" + suffix in urls["market"]
        assert "ethusdt" + suffix in urls["market"]


@pytest.mark.parametrize("symbols", [[], ["BTCUSDT", "BTCUSDT"], ["../../bad"], ["btcusdt"]])
def test_invalid_symbols_are_rejected(symbols):
    with pytest.raises(ValueError, match="symbols"):
        stream_urls(symbols)


def test_control_message_is_ignored_and_corruption_fails_closed():
    assert parse_message('{"result":null,"id":1}') is None
    assert parse_message(websocket_message()) == ("btcusdt@aggTrade", {"a": 1})
    with pytest.raises(ValueError):
        parse_message('{"stream":"btcusdt@aggTrade","data":[]}')
    with pytest.raises(ValueError):
        parse_message('{"stream":"btcusdt@aggTrade","data":{"x":NaN}}')


def test_capture_records_connection_loss_and_recovers(tmp_path):
    sessions = [
        [websocket_message(event=10), ConnectionError("wire cut")],
        [websocket_message(event=11)],
    ]
    connections = []

    def connector(url):
        connections.append(url)
        return FakeSession(sessions.pop(0) if sessions else [])

    clock = FakeClock()

    async def immediate_sleep(_seconds):
        return None

    result = asyncio.run(
        capture_feed(
            "market",
            "fake://market",
            tmp_path,
            "session",
            6.0,
            connector=connector,
            clock=clock,
            timestamp=lambda: 1_000_000,
            sleep=immediate_sleep,
        )
    )

    assert len(connections) >= 2
    assert result["reconnects"] >= 1
    assert result["counts"]["btcusdt@aggTrade"] == 2
    records = [json.loads(line) for p in tmp_path.glob("*.jsonl") for line in p.read_text().splitlines()]
    trade_ids = [x["data"]["a"] for x in records if x["stream"] == "btcusdt@aggTrade"]
    statuses = [x["data"]["event"] for x in records if x["stream"] == "collector.status"]
    assert trade_ids == [10, 11]
    assert "connected" in statuses and "disconnected" in statuses
    assert not list(tmp_path.glob("*.open"))


def test_invalid_message_logged_without_breaking_capture(tmp_path):
    def connector(_url):
        return FakeSession(['{"stream":"x","data":[]}', '{"result":null}', websocket_message()])

    result = asyncio.run(
        capture_feed(
            "market",
            "fake://market",
            tmp_path,
            "session",
            4.0,
            connector=connector,
            clock=FakeClock(),
            timestamp=lambda: 123,
        )
    )
    assert result["invalid"] == 1
    assert result["counts"]["btcusdt@aggTrade"] == 1
    records = [json.loads(line) for p in tmp_path.glob("*.jsonl") for line in p.read_text().splitlines()]
    events = [x["data"]["event"] for x in records if x["stream"] == "collector.status"]
    assert "invalid_message" in events and "control_message" in events


def test_runner_recovers_old_segment_before_capture(tmp_path):
    old = tmp_path / "market-old-0000.jsonl.open"
    old.write_text(json.dumps({"received_ns": 1, "stream": "x", "data": {}}) + "\n")
    results = asyncio.run(run_capture(tmp_path, ["BTCUSDT"], 0.00001, connector=lambda _: FakeSession([])))
    assert len(results) == 2
    assert (tmp_path / "market-old-0000.recovered.jsonl").exists()
    assert (tmp_path / "market-old-0000.recovered.jsonl.recovery.json").exists()


def test_simultaneous_capture_fails_closed(tmp_path):
    lock_path = Path(tmp_path) / ".capture.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another market capture"):
            asyncio.run(run_capture(tmp_path, ["BTCUSDT"], 0.01, connector=lambda _: None))


def test_deliberate_disconnect_reconnects_and_logs_gap(tmp_path):
    sessions = [
        [websocket_message(event=10), websocket_message(event=11)],
        [websocket_message(event=12)],
    ]
    connections = []

    def connector(url):
        connections.append(url)
        return FakeSession(sessions.pop(0) if sessions else [])

    async def immediate_sleep(_seconds):
        return None

    result = asyncio.run(
        capture_feed(
            "market",
            "fake://market",
            tmp_path,
            "session",
            12.0,
            connector=connector,
            clock=FakeClock(),
            timestamp=lambda: 123,
            sleep=immediate_sleep,
            fault_disconnect_after=2,
        )
    )
    assert len(connections) >= 2
    assert result["reconnects"] >= 1
    assert result["counts"]["btcusdt@aggTrade"] == 3
    records = [json.loads(line) for p in tmp_path.glob("*.jsonl") for line in p.read_text().splitlines()]
    trade_ids = [x["data"]["a"] for x in records if x["stream"] == "btcusdt@aggTrade"]
    statuses = [x["data"]["event"] for x in records if x["stream"] == "collector.status"]
    assert trade_ids == [10, 11, 12]
    assert statuses.count("fault_injected_disconnect") == 1
    assert "disconnected" in statuses
    assert statuses.count("connected") >= 2


def test_invalid_injection_threshold_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="fault_disconnect_after"):
        asyncio.run(run_capture(tmp_path, ["BTCUSDT"], 1, connector=lambda _: None, fault_disconnect_after=0))


def test_insufficient_disk_blocks_before_opening_connection(tmp_path):
    calls = []

    def connector(url):
        calls.append(url)
        return FakeSession([])

    with pytest.raises(LowDiskSpace, match="recording stopped"):
        asyncio.run(
            run_capture(
                tmp_path,
                ["BTCUSDT"],
                10,
                connector=connector,
                min_free_mib=512,
                free_bytes=lambda _: 0,
            )
        )
    assert calls == []
    assert not list(tmp_path.glob("*.jsonl*"))


def test_disk_guard_polls_only_after_interval_and_then_fails(tmp_path):
    now = [0.0]
    free = [1024 * 1048576]
    checks = []

    def available_bytes(path):
        checks.append(path)
        return free[0]

    guard = DiskSpaceGuard(
        tmp_path,
        min_free_mib=512,
        interval_s=2.0,
        clock=lambda: now[0],
        free_bytes=available_bytes,
    )
    guard.check(force=True)
    assert len(checks) == 1
    now[0] = 1.0
    free[0] = 0
    guard.check()
    assert len(checks) == 1
    now[0] = 2.0
    with pytest.raises(LowDiskSpace):
        guard.check()
    assert len(checks) == 2


def test_disk_exhaustion_is_fatal_but_finalizes_existing_journal(tmp_path):
    class FailingGuard:
        checks = 0

        def check(self):
            self.checks += 1
            if self.checks >= 4:
                raise LowDiskSpace("simulated full disk")

    calls = []

    def connector(url):
        calls.append(url)
        return FakeSession([websocket_message(event=1), websocket_message(event=2)])

    with pytest.raises(LowDiskSpace, match="simulated full"):
        asyncio.run(
            capture_feed(
                "market",
                "fake://market",
                tmp_path,
                "session",
                10.0,
                connector=connector,
                clock=FakeClock(),
                timestamp=lambda: 123,
                disk_guard=FailingGuard(),
            )
        )
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.open"))
    records = [json.loads(line) for p in tmp_path.glob("*.jsonl") for line in p.read_text().splitlines()]
    assert [r["data"]["a"] for r in records if r["stream"] == "btcusdt@aggTrade"] == [1]


def test_disk_reserve_cannot_be_disabled_below_minimum(tmp_path):
    with pytest.raises(ValueError, match="at least 64"):
        DiskSpaceGuard(tmp_path, min_free_mib=0)
    with pytest.raises(ValueError, match="at least 64"):
        asyncio.run(
            run_capture(
                tmp_path,
                ["BTCUSDT"],
                1,
                connector=lambda _: FakeSession([]),
                min_free_mib=32,
            )
        )
