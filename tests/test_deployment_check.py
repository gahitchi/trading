"""tools/deployment_check.py: published ports, secret permissions, paper-only settings."""

import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "deployment_check.py"
spec = importlib.util.spec_from_file_location("deployment_check", TOOL)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

COMPOSE = """services:
  ib-gateway:
    environment:
      TRADING_MODE: paper
    ports:
      - "{port}"
      - "127.0.0.1:5900:5900"
    healthcheck:
      test: ["x"]
"""


def _setup(tmp_path, port="127.0.0.1:4002:4004", mode=0o600, account="DU1234567"):
    (tmp_path / "docker-compose.yml").write_text(COMPOSE.format(port=port))
    (tmp_path / "secrets").mkdir()
    secret = tmp_path / "secrets" / "tws_password"
    secret.write_text("pw")
    secret.chmod(mode)
    (tmp_path / ".env").write_text(f"TWS_USERID=me\nLAB_PAPER_ACCOUNT={account}\nLAB_TELEGRAM_TOKEN=t\n")
    return {(name): status for status, name, _ in check.run(tmp_path) if not name.startswith("listening")}


def test_a_safe_setup_passes(tmp_path):
    results = _setup(tmp_path)
    assert "FAIL" not in results.values(), results


def test_a_public_port_a_readable_password_and_a_live_account_fail(tmp_path):
    results = _setup(tmp_path, port="4002:4004", mode=0o644, account="U1234567")
    assert results["docker-compose.yml port 4002:4004"] == "FAIL"
    assert results["secrets/tws_password"] == "FAIL"
    assert results["paper account"] == "FAIL"
