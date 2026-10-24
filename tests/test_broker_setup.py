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
        assert code == 200 and state == {"configured": False, "account": None, "installation": installation}
        assert cors["Access-Control-Allow-Origin"] == headers["Origin"]
        bad = {"username": "paper-user", "account": "U1234567", "password": "first-secret"}
        assert request("POST", "/connect", bad)[0] == 400
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
        assert (tmp_path / ".env").read_text() == "TWS_USERID=paper-user\nLAB_PAPER_ACCOUNT=DU1234567\n"
        assert (tmp_path / "secrets/tws_password").read_text() == "first-secret"
        assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "secrets/tws_password").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
        code, state, _ = request("GET", "/status")
        assert code == 200 and state == {"configured": True, "account": "DU1234567", "installation": installation}
        assert "first-secret" not in json.dumps(state) and "paper-user" not in json.dumps(state)
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
