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


def test_first_launch_opens_full_gui_without_broker_credentials_or_gateway(tmp_path, monkeypatch):
    tool = installer()
    monkeypatch.setattr(tool, "__file__", str(tmp_path / "tools/start_app.py"))
    calls = []
    opened = []
    helpers = []
    monkeypatch.setattr(tool.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(tool, "ensure_docker", lambda: None)
    monkeypatch.setattr(tool, "running_app", lambda root: False)
    monkeypatch.setattr(tool, "install_shortcuts", lambda root: None)
    monkeypatch.setattr(tool, "start_setup_service", helpers.append)
    monkeypatch.setattr(tool, "wait_until_ready", lambda url: True)
    monkeypatch.setattr(tool, "open_app", opened.append)
    assert tool.main() == 0
    assert opened == ["http://127.0.0.1:8765"]
    assert helpers == [tmp_path]
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "secrets").exists()
    assert calls[-1][0] == ["docker", "compose", "-f", "compose.app.yml", "up", "--build", "-d", "lab"]
    assert calls[-1][1]["env"]["LAB_APP_UID"] == str(os.getuid())
    # Reopening the shortcut does not recreate the running GUI or a Gateway.
    monkeypatch.setattr(tool, "running_app", lambda root: True)
    assert tool.main() == 0
    assert len(calls) == 3  # Compose version is probed at each launch.
    assert sum("up" in cmd for cmd, _ in calls) == 1
    assert all("ib-gateway" not in cmd for cmd, _ in calls)


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
    assert "launch_windows.ps1" in (root / "start-app.cmd").read_text()


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


def test_windows_installer_never_trusts_dangling_py_launcher():
    r"""Regression for 'Unable to create process using ... OneDrive\Desktop\python.exe'."""
    root = Path(__file__).parents[1]
    bootstrap = (root / "install.ps1").read_text()
    shortcut = (root / "start-app.cmd").read_text()
    launcher = (root / "tools/launch_windows.ps1").read_text()
    assert "py tools\\start_app.py" not in bootstrap + shortcut
    assert "launch_windows.ps1" in bootstrap and "launch_windows.ps1" in shortcut
    assert "Find-WorkingPython" in launcher and "sys.executable" in launcher
    assert "Get-Command py.exe" in launcher and '@("-3.13")' in launcher
    assert "Python.Python.3.13" in launcher
    assert "Ensure-Python" in launcher and "start_app.py" in launcher


def test_windows_updater_is_version_pinned_and_preserves_private_state():
    root = Path(__file__).parents[1]
    script = (root / "tools/launch_windows.ps1").read_text()
    bootstrap = (root / "install.ps1").read_text()
    assert "api.github.com/repos/$Repository/commits/main" in script
    assert "archive/$sha.zip" in script and "archive/$revision.zip" in bootstrap
    assert ".tradinglab-version" in script and ".tradinglab-version" in bootstrap
    for path in (".env", "secrets", "results", "data", "research", "lab.toml"):
        assert ('"' + path + '"') in script
    assert "App-IsRunning" in script
    assert "ps --status running --services" in script
    assert "Rollback" in script or "rollback" in script
    assert "TradingLab-backup-" in script
    assert "CheckOnly" in script and "UpdateOnly" in script


def test_reopening_windows_app_does_not_rebuild_running_docker_node(tmp_path, monkeypatch):
    """A desktop shortcut must not restart an autonomous paper bot when reopening its UI."""
    tool = installer()
    monkeypatch.setattr(tool, "__file__", str(tmp_path / "tools/start_app.py"))
    (tmp_path / ".env").write_text("TWS_USERID=x\nLAB_PAPER_ACCOUNT=DU1234567\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/tws_password").write_text("secret")
    calls = []

    class Proc:
        returncode = 0
        stdout = "lab\n"

    def run(args, **kwargs):
        calls.append(args)
        return Proc()

    monkeypatch.setattr(tool.subprocess, "run", run)
    monkeypatch.setattr(tool, "ensure_docker", lambda: None)
    monkeypatch.setattr(tool, "install_shortcuts", lambda root: None)
    monkeypatch.setattr(tool, "start_setup_service", lambda root: None)
    monkeypatch.setattr(tool, "wait_until_ready", lambda url: True)
    monkeypatch.setattr(tool, "open_app", lambda url: None)
    assert tool.main() == 0
    assert not any("up" in args for args in calls)
    assert any(args[-3:] == ["--status", "running", "--services"] for args in calls)


def test_python_startup_failure_is_private_rotating_and_sanitized(tmp_path, monkeypatch):
    tool = installer()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    try:
        raise RuntimeError("Paper DU1234567 password=dont-share-this")
    except RuntimeError as exc:
        path = tool.record_launch_failure(exc)
    content = path.read_text()
    assert "RuntimeError" in content and "test_python_startup_failure" in content
    assert "[PAPER_ACCOUNT]" in content and "password=[REDACTED]" in content
    assert "DU1234567" not in content and "dont-share-this" not in content
    assert path.stat().st_mode & 0o777 == 0o600
    # A stale large log is rotated on the next crash.
    path.write_text("X" * 1_000_000)
    try:
        raise RuntimeError("second")
    except RuntimeError as exc:
        tool.record_launch_failure(exc)
    assert (path.parent / "crash.log.1").exists()
    assert "second" in path.read_text()


def test_python_diagnostic_masks_unsupported_simulation_account_ids(tmp_path, monkeypatch):
    tool = installer()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    try:
        raise RuntimeError("Broker DUR987654 not allowed")
    except RuntimeError as exc:
        log_path = tool.record_launch_failure(exc)
    assert "DUR987654" not in log_path.read_text()
    assert "[PAPER_ACCOUNT]" in log_path.read_text()


def test_python_crash_log_rejects_symlink(tmp_path, monkeypatch):
    tool = installer()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tool.diagnostic_log()
    path.unlink()
    outside = tmp_path / "private"
    path.symlink_to(outside)
    with pytest.raises(OSError, match="symbolic link"):
        tool.record_launch_failure(RuntimeError("should not write"))
    assert not outside.exists()
