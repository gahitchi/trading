"""Offline GUI broker onboarding: same-origin restrictions and private paper credentials."""

import hashlib
import http.client
import importlib.util
import json
import threading
from pathlib import Path


def broker_module():
    path = Path(__file__).parents[1] / "tools" / "broker_setup.py"
    spec = importlib.util.spec_from_file_location("broker_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gui_broker_setup_is_strictly_local_and_paper_only(tmp_path, monkeypatch):
    module = broker_module()
    for name in ("compose.app.yml", "compose.gateway.yml"):
        (tmp_path / name).write_text("no broker")
    gateway_calls = []
    monkeypatch.setattr(module, "start_gateway", lambda root: gateway_calls.append(root) or True)
    server = module.SetupServer(("127.0.0.1", 0), tmp_path)
    monkeypatch.setattr(module, "PORT", server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    headers = {
        "Host": f"127.0.0.1:{port}",
        "Origin": "http://127.0.0.1:8765",
        "Content-Type": "application/json",
    }

    def request(method, path, data=None, overrides=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        body = json.dumps(data) if data is not None else None
        conn.request(method, path, body, {**headers, **(overrides or {})})
        response = conn.getresponse()
        raw = response.read()
        result = response.status, json.loads(raw) if raw else None, dict(response.getheaders())
        conn.close()
        return result

    try:
        code, state, cors = request("GET", "/status")
        installation = hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest()
        assert code == 200 and state == {
            "configured": False,
            "account": None,
            "mode": "paper",
            "order_setup_enabled": False,
            "installation": installation,
            "protocol_version": module.SETUP_PROTOCOL_VERSION,
        }
        assert cors["Access-Control-Allow-Origin"] == headers["Origin"]
        bad = {"username": "DUR7654321", "account": "U1234567", "password": "first-secret"}
        assert request("POST", "/connect", bad)[0] == 400
        # DUR free-trial simulation IDs are tested in a separate read-only onboarding test.
        assert not (tmp_path / ".env").exists()
        good = {**bad, "account": "DU1234567"}
        for override in [
            {"Origin": "https://evil.example"},
            {"Origin": ""},
            {"Host": "evil.example"},
            {"Content-Type": "text/plain"},
        ]:
            assert request("POST", "/connect", good, override)[0] in (403, 415)
        assert not (tmp_path / ".env").exists()
        assert not (tmp_path / "secrets/tws_password").exists()
        assert request("OPTIONS", "/connect")[0] == 204
        assert request("POST", "/connect", good)[0] == 200
        assert gateway_calls == [tmp_path]
        assert (tmp_path / ".env").read_text() == "TWS_USERID=DUR7654321\nLAB_PAPER_ACCOUNT=DU1234567\n"
        assert (tmp_path / "secrets/tws_password").read_text() == "first-secret"
        assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "secrets/tws_password").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
        code, state, _ = request("GET", "/status")
        assert code == 200 and state == {
            "configured": True,
            "account": "DU1234567",
            "mode": "paper",
            "order_setup_enabled": True,
            "installation": installation,
            "protocol_version": module.SETUP_PROTOCOL_VERSION,
        }
        assert "first-secret" not in json.dumps(state) and "DUR7654321" not in json.dumps(state)
        assert request("POST", "/connect", good)[0] == 400
        assert request("POST", "/retry", {})[0] == 200
        assert gateway_calls == [tmp_path, tmp_path]
        assert request("POST", "/retry", {"arbitrary": True})[0] == 400
        assert (tmp_path / "secrets/tws_password").read_text() == "first-secret"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_password_never_passes_as_docker_arguments_or_environment(tmp_path, monkeypatch):
    module = broker_module()
    (tmp_path / "secrets").mkdir()
    (tmp_path / ".env").write_text("TWS_USERID=user\nLAB_PAPER_ACCOUNT=DU1234567\n")
    (tmp_path / "secrets/tws_password").write_text("PRIVATE")
    called = []

    class Result:
        returncode = 0

    def call(argv, **kw):
        called.append((argv, kw))
        return Result()

    monkeypatch.setattr(module.subprocess, "run", call)
    assert module.start_gateway(tmp_path)
    assert called[0][0][-2:] == ["--no-deps", "ib-gateway"]
    assert "PRIVATE" not in str(called)
    assert called[0][1]["stdin"] == module.subprocess.DEVNULL
    assert called[0][1]["stdout"] == module.subprocess.DEVNULL
    assert called[0][1]["stderr"] == module.subprocess.DEVNULL


def test_partial_or_symlinked_credentials_never_overwritten(tmp_path):
    module = broker_module()
    payload = {"username": "paper-user", "account": "DU1234567", "password": "strong-secret"}
    (tmp_path / "secrets").mkdir()
    external = tmp_path / "external"
    external.write_text("do not change")
    (tmp_path / "secrets/tws_password").symlink_to(external)
    try:
        module.save_credentials(tmp_path, payload)
    except ValueError as exc:
        assert "already stored" in str(exc)
    else:
        raise AssertionError("Expected symlinked secret to block")
    assert external.read_text() == "do not change"
    assert not (tmp_path / ".env").exists()


def test_trial_account_uses_read_only_gateway_and_never_enables_trading(tmp_path, monkeypatch):
    module = broker_module()
    for name in ("compose.app.yml", "compose.gateway.yml", "compose.gateway.demo.yml"):
        (tmp_path / name).write_text("test")
    demo = {"username": "trial-login", "account": "DUR987654", "password": "trial-secret"}
    assert module.save_credentials(tmp_path, demo) == "DUR987654"
    assert module.saved_account(tmp_path) == "DUR987654"

    commands = []

    class Result:
        returncode = 0

    def fake_run(args, **kwargs):
        commands.append(list(args))
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.start_gateway(tmp_path)
    assert "-f" in commands[0]
    assert "compose.gateway.demo.yml" in commands[0]
    assert commands[0][-2:] == ["--no-deps", "ib-gateway"]
    assert "trial-secret" not in str(commands)
    # The runtime trading guard remains DU-only irrespective of GUI onboarding.
    from lab.safety import SafetyError, check_paper_settings, is_paper_account

    assert not is_paper_account("IB-DUR987654")
    try:
        check_paper_settings(4004, "DUR987654")
    except SafetyError:
        pass
    else:
        raise AssertionError("Demo identifier bypassed the paper-account safety guard")

    server = module.SetupServer(("127.0.0.1", 0), tmp_path)
    monkeypatch.setattr(module, "PORT", server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    try:
        conn.request(
            "GET",
            "/status",
            headers={
                "Host": f"127.0.0.1:{server.server_address[1]}",
                "Origin": "http://127.0.0.1:8765",
            },
        )
        response = conn.getresponse()
        state = json.loads(response.read())
        assert response.status == 200
        assert state["configured"] is True and state["mode"] == "demo"
        assert state["order_setup_enabled"] is False
        assert "trial-login" not in json.dumps(state)
        assert "trial-secret" not in json.dumps(state)
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_trial_http_onboarding_accepts_dur_in_readonly_status(tmp_path, monkeypatch):
    module = broker_module()
    for name in ("compose.app.yml", "compose.gateway.yml", "compose.gateway.demo.yml"):
        (tmp_path / name).write_text("test")
    calls = []
    monkeypatch.setattr(module, "start_gateway", lambda root: calls.append(root) or True)
    server = module.SetupServer(("127.0.0.1", 0), tmp_path)
    monkeypatch.setattr(module, "PORT", server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {
        "Host": f"127.0.0.1:{server.server_address[1]}",
        "Origin": "http://127.0.0.1:8765",
        "Content-Type": "application/json",
    }
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        creds = {"username": "free-trial-user", "account": "DUR987654", "password": "test-secret"}
        conn.request("POST", "/connect", body=json.dumps(creds), headers=headers)
        response = conn.getresponse()
        result = json.loads(response.read())
        assert response.status == 200
        assert result == {
            "configured": True,
            "account": "DUR987654",
            "protocol_version": module.SETUP_PROTOCOL_VERSION,
            "mode": "demo",
            "order_setup_enabled": False,
        }
        assert calls == [tmp_path]
        assert (tmp_path / "secrets/tws_password").stat().st_mode & 0o777 == 0o600
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
