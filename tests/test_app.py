"""Local UI: broker-data interlock, operator intent and browser trust boundary."""

import http.client
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from lab import app
from lab.authority import Authority, digest
from lab.persistence import atomic_json, read_json
from lab.safety import SafetyError, clear_kill_switch, read_kill_switch, set_kill_switch


@pytest.fixture
def controller(tmp_path, monkeypatch):
    for key in app.CONNECTION_ENV:
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "lab.toml"
    config.write_text((Path(app.__file__).parent / "default.toml").read_text())
    c = app.Controller(config)
    c.settings["account"] = "DU1234567"
    c.configure(c.settings)
    return c


def prepared(c):
    c.process = Mock()
    c.process.poll.return_value = None
    identity = {"tickers": c.settings["tickers"], "account": c.settings["account"]}
    atomic_json(c.rt / "identity.json", identity)
    Authority(c.rt, identity).issue("supervised")
    set_kill_switch(c.rt / "KILL", "halt", c.reason)
    stamp = app.latest_completed_close(datetime.now(UTC), c.cfg.trading.data_delay_min)
    atomic_json(
        c.rt / "heartbeat.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "running": True,
            "account": "IB-DU1234567",
            "hard_halt": None,
            "halted": False,
            "blocked": None,
        },
    )
    atomic_json(
        c.rt / "data-readiness.json",
        {
            "session": c.session,
            "source": "ibkr",
            "binding": digest(identity),
            "errors": [],
            "signals": {
                f"{s}:{t}": {"timestamp": stamp, "active": True}
                for s in app.STRATEGIES[c.settings["strategy"]]
                for t in c.settings["tickers"]
            },
            "history": {t: list(range(c.cfg.portfolio.vol_window + 1)) for t in c.settings["tickers"]},
        },
    )
    c.phase = "preparing"
    c.deadline = time.monotonic() + 180


@pytest.mark.parametrize(
    "change",
    [
        {"account": "U1234567"},
        {"port": 4001},
        {"capital": float("nan")},
        {"hours": 169},
        {"tickers": ["SPY", "SPY"]},
        {"tickers": ["UNKNOWN"]},
        {"strategy": "unreviewed"},
        {"mode": "live"},
        {"gold": "yes"},
    ],
)
def test_invalid_setup_never_changes_config(controller, change):
    before = controller.config_path.read_bytes()
    with pytest.raises((ValueError, SafetyError)):
        controller.configure({**controller.settings, **change})
    assert controller.config_path.read_bytes() == before


def test_saved_setup_is_bound_to_config_and_ignores_connection_environment(controller, monkeypatch):
    monkeypatch.setenv("LAB_PAPER_ACCOUNT", "DU7654321")
    monkeypatch.setenv("LAB_IB_PORT", "7497")
    restored = app.Controller(controller.config_path)
    assert restored.cfg.paper.account_id == "DU1234567"
    assert restored.cfg.paper.port == 4002
    controller.config_path.write_text(controller.config_path.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="configuration differ"):
        restored.start()


def test_gold_is_explicit_and_cannot_change_an_existing_runtime(controller):
    legacy = {key: value for key, value in controller.settings.items() if key != "gold"}
    assert app.validate_settings(legacy, controller.cfg.universe)["gold"] is False
    atomic_json(controller.rt / "identity.json", {"account": controller.settings["account"]})
    with pytest.raises(ValueError, match="migration"):
        controller.configure({**controller.settings, "gold": True})


def test_gold_is_presented_as_a_selectable_commodity_tile():
    page = (app.UI / "index.html").read_text()
    heading = page.index("Futures &amp; Commodities to trade")
    tile = page.index('name="gold"', heading)

    assert page.index('<div class="symbols">', heading) < tile
    assert "<span>XAU</span>" in page[tile : tile + 160]
    assert 'aria-label="Trade London Gold XAUUSD"' in page[tile : tile + 160]


def test_confirmed_gold_migration_preserves_plan_and_uses_broker_checked_helper(controller, monkeypatch):
    atomic_json(controller.rt / "identity.json", {"account": controller.settings["account"]})
    calls = []
    monkeypatch.setattr(
        "lab.gold_migration.enable_gold",
        lambda cfg, tickers, private_dir, reason: calls.append((cfg, tickers, private_dir, reason)),
    )

    controller.configure({**controller.settings, "gold": True}, confirm_gold_migration=True)

    assert controller.settings["gold"] is True
    assert calls[0][1] == controller.settings["tickers"]
    assert calls[0][2].name == "private-backups"
    assert calls[0][3] == controller.reason


def test_delayed_feed_schedule_waits_for_completed_data(controller):
    assert controller.cfg.trading.data_delay_min == 15
    assert controller.cfg.paper.signal_time == "16:20"
    assert controller.cfg.paper.rebalance_time == "16:25"
    controller.configure({**controller.settings, "data": "REALTIME"})
    assert controller.cfg.trading.data_delay_min == 0
    assert controller.cfg.paper.signal_time == "16:10"
    assert controller.cfg.paper.rebalance_time == "16:15"


def test_start_requires_saved_setup_and_spawns_paused_paper_engine(controller, monkeypatch):
    calls = []

    def spawn(args, **kw):
        assert read_kill_switch(controller.rt / "KILL")["mode"] == "halt"
        process = Mock()
        process.poll.return_value = None
        process.stdout = iter(())
        calls.append((args, kw))
        return process

    monkeypatch.setattr(app.subprocess, "Popen", spawn)
    monkeypatch.setattr(controller, "_logs", lambda process: None)
    previous_session = controller.session
    controller.start()
    args, kw = calls[0]
    assert "paper" in args and args[args.index("--mode") + 1] == "supervised"
    assert kw["env"]["LAB_APP_SESSION"] == controller.session != previous_session
    assert all(key not in kw["env"] for key in app.CONNECTION_ENV)
    controller.tick()
    assert controller.phase == "preparing" and read_kill_switch(controller.rt / "KILL")
    controller.process = None
    controller.settings_path.unlink()
    with pytest.raises(ValueError, match="Save your setup"):
        controller.start()
    assert len(calls) == 1


def test_explicit_resume_restarts_with_new_session_and_keeps_stop_during_reload(controller, monkeypatch):
    prepared(controller)
    old = controller.process
    old.pid = 987654
    controller.pause()
    new = Mock()
    new.poll.return_value = None
    new.stdout = iter(())
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **kw: new)
    monkeypatch.setattr(controller, "_logs", lambda process: None)
    kill = Mock()
    monkeypatch.setattr(app.os, "killpg", kill)
    previous = controller.session
    controller.start()
    kill.assert_called_once_with(old.pid, app.signal.SIGINT)
    old.wait.assert_called_once_with(timeout=15)
    assert controller.session != previous and controller.process is new
    controller.tick()
    assert controller.phase == "preparing" and read_kill_switch(controller.rt / "KILL")
    assert "this broker session" in controller.message


@pytest.mark.parametrize(
    "file,key,value",
    [
        ("data-readiness", "source", "simulated"),
        ("data-readiness", "session", "old-session"),
        ("data-readiness", "binding", "other-account"),
        ("data-readiness", "errors", ["strategy failed"]),
        ("data-readiness", "signals", {}),
        ("data-readiness", "history", {"SPY": [1], "QQQ": [1]}),
        ("heartbeat", "running", False),
        ("heartbeat", "account", "IB-DU7654321"),
        ("heartbeat", "hard_halt", "reconciliation"),
        ("heartbeat", "blocked", "cash mismatch"),
        ("heartbeat", "at", "2000-01-01T00:00:00+00:00"),
    ],
)
def test_unqualified_startup_evidence_cannot_enable_orders(controller, file, key, value):
    prepared(controller)
    path = controller.rt / (file + ".json")
    record = read_json(path)
    record[key] = value
    atomic_json(path, record)
    controller.tick()
    assert controller.phase == "preparing"
    assert read_kill_switch(controller.rt / "KILL")
    assert Authority(controller.rt, read_json(controller.rt / "identity.json")).read()["mode"] == "supervised"
    controller.deadline = 0
    controller.tick()
    assert controller.phase == "blocked" and "permissions" in controller.message


def test_yesterdays_weekday_bars_cannot_pass_startup(controller):
    prepared(controller)
    path = controller.rt / "data-readiness.json"
    record = read_json(path)
    for row in record["signals"].values():
        row["timestamp"] -= 86_400 * 10**9
    atomic_json(path, record)
    controller.tick()
    assert controller.phase == "preparing" and read_kill_switch(controller.rt / "KILL")


def test_complete_broker_evidence_activates_only_selected_authority_then_pause_sticks(controller):
    prepared(controller)
    controller.tick()
    assert controller.phase == "running" and not read_kill_switch(controller.rt / "KILL")
    a = Authority(controller.rt, read_json(controller.rt / "identity.json"))
    assert a.read()["mode"] == "autonomous"
    controller.pause()
    controller.tick()
    assert controller.phase == "paused" and read_kill_switch(controller.rt / "KILL")


def test_expired_permission_pauses_and_requires_explicit_restart(controller, monkeypatch):
    prepared(controller)
    controller.tick()
    a = Authority(controller.rt, read_json(controller.rt / "identity.json"))
    expires = a.read()["expires"]
    monkeypatch.setattr("lab.authority.time.time", lambda: expires + 1)
    controller.tick()
    assert controller.phase == "blocked" and read_kill_switch(controller.rt / "KILL")


def test_external_stop_or_flatten_is_never_cleared_or_downgraded(controller):
    prepared(controller)
    external = set_kill_switch(controller.rt / "KILL", "halt", "operator review")
    controller.tick()
    assert controller.phase == "blocked"
    controller.pause()
    assert read_kill_switch(controller.rt / "KILL") == external
    with pytest.raises(ValueError, match="external stop"):
        controller.pause(flatten=True)
    clear_kill_switch(controller.rt / "KILL")
    controller.pause(flatten=True)
    controller.pause()
    assert read_kill_switch(controller.rt / "KILL")["mode"] == "flatten"
    with pytest.raises(ValueError, match="exit is in progress"):
        controller.start()


def test_stop_changed_during_activation_remains_set(controller, monkeypatch):
    prepared(controller)
    clear = app.clear_kill_switch

    def competing_stop(path, **kw):
        set_kill_switch(path, "flatten", "external")
        return clear(path, **kw)

    monkeypatch.setattr(app, "clear_kill_switch", competing_stop)
    controller.tick()
    assert controller.phase == "blocked"
    assert read_kill_switch(controller.rt / "KILL")["reason"] == "external"


def test_saved_plan_with_history_requires_reviewed_migration(controller):
    prepared(controller)
    controller.process = None
    with pytest.raises(ValueError, match="reviewed plan migration"):
        controller.configure({**controller.settings, "capital": 25000})
    controller.configure({**controller.settings, "hours": 12})
    assert controller.settings["hours"] == 12
    assert not controller.status()["snapshot_current"]


def test_latest_completed_close_accounts_for_early_close_holiday_and_delay():
    # 2026 Thanksgiving Friday closes at 18:00 UTC; delayed feed becomes eligible at 18:15.
    before = datetime(2026, 11, 27, 18, 14, tzinfo=UTC)
    after = before + timedelta(minutes=1)
    assert datetime.fromtimestamp(app.latest_completed_close(before, 15) / 1e9, UTC) == datetime(
        2026, 11, 25, 21, tzinfo=UTC
    )
    assert datetime.fromtimestamp(app.latest_completed_close(after, 15) / 1e9, UTC) == datetime(
        2026, 11, 27, 18, tzinfo=UTC
    )


def test_http_control_rejects_cross_origin_requests_and_requires_token(controller):
    server = app.AppServer(("127.0.0.1", 0), controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    headers = {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "X-App-Token": server.token,
        "Content-Type": "application/json",
    }

    def request(method, path, body=None, overrides=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request(method, path, body, {**headers, **(overrides or {})})
        response = conn.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return result

    try:
        code, body, response_headers = request("GET", "/")
        assert code == 200 and server.token.encode() in body
        assert "frame-ancestors 'none'" in response_headers["Content-Security-Policy"]
        for overrides in [
            {"Host": "evil.example"},
            {"Origin": "https://evil.example"},
            {"X-App-Token": "wrong"},
            {"Content-Type": "text/plain"},
        ]:
            assert request("POST", "/api/start", "{}", overrides)[0] == 403
        assert not controller.alive() and not (controller.rt / "KILL").exists()
        assert request("GET", "/api/status", overrides={"X-App-Token": ""})[0] == 403
        assert request("GET", "/../lab.toml")[0] == 404
        assert request("POST", "/api/setup", "x" * 17000)[0] == 400
        assert request("POST", "/api/exit", "{}")[0] == 400
        assert request("POST", "/api/setup", json.dumps(controller.settings))[0] == 200
        assert request("GET", "/app.js")[0] == 200
        # Everything the page and its manifest reference is served, with the right type, under the same CSP.
        page = body.decode()
        manifest = json.loads(request("GET", "/manifest.webmanifest")[1])
        refs = set(re.findall(r'(?:href|src)="(/[^"]+)"', page)) | {i["src"] for i in manifest["icons"]}
        refs |= set(re.findall(r'url\("(/[^"]+)"\)', request("GET", "/app.css")[1].decode()))
        assert {"/icons/icon.svg", "/fonts/archivo-latin.woff2", "/manifest.webmanifest"} <= refs
        for ref in refs:
            code, raw, served = request("GET", ref)
            assert code == 200 and raw and served["Content-Type"] == app.UI_FILES[ref][1], ref
            assert served["Content-Security-Policy"].startswith("default-src 'self'")
        assert request("GET", "/icons/../app.py")[0] == 404 and request("GET", "/fonts/OFL.txt")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_readiness_tracks_only_complete_positive_data_and_latches_strategy_failure(tmp_path):
    from lab.agents.messages import PriceMark, RiskEvent, SignalUpdate
    from lab.agents.readiness import ReadinessAgent, ReadinessConfig

    path = tmp_path / "readiness.json"
    agent = ReadinessAgent(
        ReadinessConfig(
            path=str(path), session="s", binding="b", sleeves={"one:SPY": "SPY"}, window=2, source="simulated"
        )
    )
    for ts in range(1, 5):
        agent.mark(PriceMark("SPY", 100.0, ts))
    agent.mark(PriceMark("SPY", -1.0, 5))
    agent.mark(PriceMark("SPY", 100.0, 2))
    agent.signal(SignalUpdate("one:SPY", "SPY", 0.5, 100.0, 4))
    assert read_json(path)["history"]["SPY"] == [2, 3, 4]
    agent.risk(RiskEvent(4, "signal_failed", "failure"))
    agent.signal(SignalUpdate("one:SPY", "SPY", 0.5, 100.0, 4))
    assert read_json(path)["errors"]


def test_simulated_live_node_readiness_cannot_masquerade_as_broker_data(goog_lab, goog, tmp_path, monkeypatch):
    from lab.signals.registry import SignalSpec
    from lab.sim.harness import LiveHarness

    monkeypatch.setenv("LAB_APP_SESSION", "test-session")
    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=556)
    set_kill_switch(h.runtime / "KILL", "halt", "test app startup")
    result = h.session()
    assert result.error is None and not h.broker().state["orders"]
    evidence = read_json(h.runtime / "data-readiness.json")
    assert evidence["source"] == "simulated" and evidence["session"] == "test-session"
    assert evidence["binding"] == digest(read_json(h.runtime / "identity.json"))
    assert len(evidence["history"]["GOOG"]) == goog_lab.portfolio.vol_window + 1
    assert evidence["signals"]["buy_and_hold:GOOG"]["active"]
