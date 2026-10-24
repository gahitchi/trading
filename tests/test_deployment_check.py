"""tools/deployment_check.py: published ports (from the rendered compose config), secrets, paper-only settings."""

import importlib.util
import shutil
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "deployment_check.py"
spec = importlib.util.spec_from_file_location("deployment_check", TOOL)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI (compose config)")

SAFE = """services:
  ib-gateway:
    image: busybox
    environment:
      TRADING_MODE: paper
    ports:
      - "127.0.0.1:4002:4004"
      - "127.0.0.1:5900:5900"
"""


def _setup(tmp_path, compose=SAFE, name="docker-compose.yml", mode=0o600, account="DU1234567", extra=None):
    (tmp_path / name).write_text(compose)
    for fname, text in (extra or {}).items():
        (tmp_path / fname).write_text(text)
    (tmp_path / "secrets").mkdir()
    secret = tmp_path / "secrets" / "tws_password"
    secret.write_text("pw")
    secret.chmod(mode)
    (tmp_path / ".env").write_text(f"TWS_USERID=me\nLAB_PAPER_ACCOUNT={account}\nLAB_TELEGRAM_TOKEN=t\n")
    return [(status, name) for status, name, _ in check.run(tmp_path) if name != "running containers"]


@needs_docker
def test_a_safe_setup_passes(tmp_path):
    results = _setup(tmp_path)
    assert not [r for r in results if r[0] == "FAIL"], results
    assert ("PASS", "default stack: ib-gateway port 127.0.0.1:4002->4004") in results


@needs_docker
def test_an_override_file_publishing_publicly_fails(tmp_path):
    """ChatGPT C005-F3: an override file was never read, so the checker passed without seeing the port."""
    override = 'services:\n  ib-gateway:\n    ports:\n      - "8765:8765"\n'
    results = _setup(tmp_path, extra={"docker-compose.override.yml": override})
    assert ("FAIL", "default stack: ib-gateway port *:8765->8765") in results


@needs_docker
def test_inline_ports_in_compose_yaml_are_checked(tmp_path):
    """ChatGPT C005-F3: inline `ports: [...]` and compose.yaml were skipped by the text parser."""
    inline = 'services:\n  app:\n    image: busybox\n    ports: ["0.0.0.0:8765:8765"]\n'
    results = _setup(tmp_path, compose=inline, name="compose.yaml")
    assert ("FAIL", "default stack: app port 0.0.0.0:8765->8765") in results


@needs_docker
def test_a_readable_password_and_a_live_account_fail(tmp_path):
    results = dict((n, s) for s, n in _setup(tmp_path, mode=0o644, account="U1234567"))
    assert results["secrets/tws_password"] == "FAIL" and results["paper account"] == "FAIL"


def test_without_docker_the_ports_are_not_assumed_safe(tmp_path, monkeypatch):
    which = shutil.which
    monkeypatch.setattr(check.shutil, "which", lambda name: None if name == "docker" else which(name))
    results = _setup(tmp_path)
    assert ("FAIL", "default stack") in results
