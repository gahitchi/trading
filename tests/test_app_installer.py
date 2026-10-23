import importlib.util
import os
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
    monkeypatch.setattr(tool.webbrowser, "open", lambda _: True)
    assert tool.main() == 0
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert "secret-test-password" not in (tmp_path / ".env").read_text()
    assert (tmp_path / "secrets/tws_password").read_text() == "secret-test-password"
    assert calls[-1][0] == ["docker", "compose", "-f", "compose.app.yml", "up", "--build", "-d"]
    assert calls[-1][1]["env"]["LAB_APP_UID"] == str(os.getuid())
    # Repeat launch reuses credentials without prompting or overwriting them.
    assert tool.main() == 0
