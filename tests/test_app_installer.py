import importlib.util
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


def installer():
    spec = importlib.util.spec_from_file_location("start_app", Path(__file__).parents[1] / "tools/start_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_password_storage_is_private_and_cannot_replace_existing_secret(tmp_path):
    tool = installer()
    secret = tmp_path / "password"
    tool.private_file(secret, "local-test-secret")
    assert secret.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        tool.private_file(secret, "replacement")
    assert secret.read_text() == "local-test-secret"


def test_installer_creates_only_private_setup_and_launches_fixed_paper_compose(tmp_path, monkeypatch):
    tool = installer()
    monkeypatch.setattr(tool, "__file__", str(tmp_path / "tools/start_app.py"))
    values = iter(["test-user", "DU1234567"])
    monkeypatch.setattr("builtins.input", lambda _: next(values))
    monkeypatch.setattr(tool.getpass, "getpass", lambda _: "secret-test-password")
    calls = []
    monkeypatch.setattr(tool.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    opened = []
    monkeypatch.setattr(tool, "ensure_docker", lambda: None)
    monkeypatch.setattr(tool, "install_shortcuts", lambda root: None)
    monkeypatch.setattr(tool, "wait_until_ready", lambda url: True)
    monkeypatch.setattr(tool, "open_app", opened.append)
    assert tool.main() == 0
    assert opened == ["http://127.0.0.1:8765"]
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert "secret-test-password" not in (tmp_path / ".env").read_text()
    assert (tmp_path / "secrets/tws_password").read_text() == "secret-test-password"
    assert calls[-1][0] == ["docker", "compose", "-f", "compose.app.yml", "up", "--build", "-d"]
    assert calls[-1][1]["env"]["LAB_APP_UID"] == str(os.getuid())
    # Repeat launch reuses credentials without prompting or overwriting them.
    assert tool.main() == 0


def test_the_app_opens_in_its_own_window_when_edge_or_chrome_is_installed(tmp_path, monkeypatch):
    tool = installer()
    edge = tmp_path / "msedge.exe"
    edge.write_text("")
    launched, browser = [], []
    monkeypatch.setattr(tool.subprocess, "Popen", lambda args, **kw: launched.append(args))
    monkeypatch.setattr(tool.webbrowser, "open", browser.append)
    monkeypatch.setattr(tool, "APP_BROWSERS", [str(tmp_path / "missing.exe"), str(edge)])
    tool.open_app("http://127.0.0.1:8765")
    assert launched == [[str(edge), "--app=http://127.0.0.1:8765", "--window-size=1280,900"]] and not browser
    # Without one, the default browser opens the same address.
    monkeypatch.setattr(tool, "APP_BROWSERS", [str(tmp_path / "missing.exe")])
    monkeypatch.setattr(tool.shutil, "which", lambda name: None)
    tool.open_app("http://127.0.0.1:8765")
    assert browser == ["http://127.0.0.1:8765"] and len(launched) == 1


def test_windows_gets_desktop_and_start_menu_shortcuts_with_the_app_icon(tmp_path, monkeypatch):
    tool = installer()
    calls = []
    monkeypatch.setattr(tool.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    tool.install_shortcuts(tmp_path)
    assert calls == []  # no shortcut on macOS
    monkeypatch.setattr(tool.sys, "platform", "win32")
    tool.install_shortcuts(tmp_path)
    ((args, kw),) = calls
    assert args[0] == "powershell" and "'Desktop', 'Programs'" in args[-1] and "Test-Path" in args[-1]
    assert kw["check"] is False  # a failed shortcut never stops the app from opening
    assert kw["env"]["TRADING_LAB_TARGET"] == str(tmp_path / "start-app.cmd")
    assert kw["env"]["TRADING_LAB_ICON"] == str(tmp_path / "src/lab/ui/icons/trading-lab.ico")
    root = Path(__file__).parents[1]
    assert (root / "src/lab/ui/icons/trading-lab.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"  # an icon file
    assert "start_app.py" in (root / "start-app.cmd").read_text()


def test_the_window_waits_for_the_app_to_answer(monkeypatch):
    tool = installer()

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert tool.wait_until_ready(f"http://127.0.0.1:{server.server_address[1]}", seconds=5)
    finally:
        server.shutdown()
        server.server_close()
    monkeypatch.setattr(tool.time, "sleep", lambda _: None)
    assert not tool.wait_until_ready(f"http://127.0.0.1:{server.server_address[1]}", seconds=0.2)


def test_a_stopped_docker_desktop_is_started_and_awaited(tmp_path, monkeypatch):
    tool = installer()
    app = tmp_path / "Docker Desktop.exe"
    app.write_text("")
    states = iter([False, False, True])
    started = []
    monkeypatch.setattr(tool, "docker_running", lambda: next(states))
    monkeypatch.setattr(tool, "DOCKER_DESKTOP", [app])
    monkeypatch.setattr(tool.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(tool.time, "sleep", lambda _: None)
    tool.ensure_docker()
    assert started == [[str(app)]]
    monkeypatch.setattr(tool, "docker_running", lambda: False)
    monkeypatch.setattr(tool, "DOCKER_DESKTOP", [tmp_path / "missing.exe"])
    with pytest.raises(ValueError, match="systemctl start docker"):
        tool.ensure_docker()


def test_a_browser_that_fails_to_launch_falls_through_to_the_next(tmp_path, monkeypatch):
    tool = installer()
    broken, edge = tmp_path / "broken.exe", tmp_path / "msedge.exe"
    broken.write_text("")
    edge.write_text("")
    launched = []

    def popen(args, **kw):
        if args[0] == str(broken):
            raise OSError("not a valid application")
        launched.append(args[0])

    monkeypatch.setattr(tool.subprocess, "Popen", popen)
    monkeypatch.setattr(tool, "APP_BROWSERS", [str(broken), str(edge)])
    tool.open_app("http://127.0.0.1:8765")
    assert launched == [str(edge)]
