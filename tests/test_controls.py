"""Control files and health (G007-F11, F12) and order-ID reuse (G004-F4)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lab.cli import cmd_health
from lab.halt import clear_halt, read_halt
from lab.paper import heartbeat_path
from lab.safety import read_kill_switch


@pytest.mark.parametrize("content", ["[]", "7", "null", '"halt"', "{not json", '{"kind": "made_up"}', "\x00\xff"])
def test_malformed_control_files_stop_trading_instead_of_crashing(tmp_path, content):
    kill, halt = tmp_path / "KILL", tmp_path / "HALT"
    kill.write_text(content)
    halt.write_text(content)
    assert read_kill_switch(kill)["mode"] == "halt"
    h = read_halt(halt)
    assert h["kind"] == "invalid_control_file" and isinstance(h["history"], list)
    assert clear_halt(halt, "inspected and removed", audit=tmp_path / "audit.jsonl") is not None  # recoverable
    assert read_halt(halt) is None


def test_unreadable_control_file_fails_closed(tmp_path):
    d = tmp_path / "HALT"
    d.mkdir()  # reading a directory raises OSError
    assert read_halt(d)["kind"] == "invalid_control_file"
    assert read_kill_switch(d)["mode"] == "halt"


def _health(cfg, hb, ready=False):
    p = heartbeat_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(hb if isinstance(hb, str) else json.dumps(hb))
    return cmd_health(cfg, SimpleNamespace(max_age=180, ready=ready))


def test_health_distinguishes_alive_stopped_future_and_not_ready(goog_lab, capsys):
    now = datetime.now(UTC)
    fresh = {"at": now.isoformat(), "running": True}
    assert _health(goog_lab, fresh) == 0
    assert _health(goog_lab, {**fresh, "running": False}) == 1  # a clean stop is not alive (F12)
    assert _health(goog_lab, {**fresh, "at": (now + timedelta(hours=1)).isoformat()}) == 1  # from the future
    assert _health(goog_lab, {**fresh, "at": (now - timedelta(hours=1)).isoformat()}) == 1  # stale
    assert _health(goog_lab, "[]") == 1 and _health(goog_lab, "{broken") == 1
    halted = {**fresh, "hard_halt": "circuit_breaker"}
    assert _health(goog_lab, halted) == 0  # alive: a restart would not help
    assert _health(goog_lab, halted, ready=True) == 2  # but not ready to trade
    capsys.readouterr()


@pytest.mark.parametrize("bucket", ["pending", "disposed", "terminal"])
def test_a_journaled_client_order_id_is_never_sent_again(bucket):
    from lab.agents.execution import ExecutionAgent

    events, submitted = [], []

    class Factory:
        def market(self, iid, side, qty, time_in_force):
            return SimpleNamespace(client_order_id="O-REUSED")

    agent = SimpleNamespace(
        config=SimpleNamespace(live=False, session=None),
        cache=SimpleNamespace(instrument=lambda iid: SimpleNamespace(make_qty=lambda q: q)),
        order_factory=Factory(),
        persistence_failed=False,
        disposed={},
        terminal={},
        pending={"O-REUSED": {"ticker": "GOOG", "side": "BUY", "qty": 1, "ts": 0}},
        _risk_event=lambda kind, detail: events.append(kind),
        submit_order=submitted.append,
    )
    intent = agent.pending.pop("O-REUSED")
    getattr(agent, bucket)["O-REUSED"] = intent
    assert ExecutionAgent._submit(agent, "GOOG.XNAS", "GOOG", 5) is False
    assert submitted == [] and events == ["blocked"]
