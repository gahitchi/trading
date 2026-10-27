"""Guard F0 build toolchain configuration against silently ignored Actions inputs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP_UV = re.compile(
    r"(?m)^\s*- uses: astral-sh/setup-uv@[0-9a-f]{40}\s*$\n"
    r"\s+with:\s*\n"
    r'\s+version: "[0-9]+\.[0-9]+\.[0-9]+"\s*$'
)


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".github/workflows/f0-two-runner-wheel.yml"])
def test_all_f0_uv_actions_pin_supported_version_input(path):
    workflow = (ROOT / path).read_text(encoding="utf-8")
    assert "uv-version:" not in workflow
    count = workflow.count("uses: astral-sh/setup-uv@")
    assert count >= 1
    assert len(SETUP_UV.findall(workflow)) == count


def test_f0_container_security_steps_are_pinned_and_fail_closed():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610" in workflow
    assert "anchore/scan-action@1638637db639e0ade3258b51db49a9a137574c3e" in workflow
    assert "syft-version: v1.55.0" in workflow
    assert "grype-version: v0.121.0" in workflow
    assert "severity-cutoff: high" in workflow
    assert "fail-build: true" in workflow
    assert "subject-path: ${{ runner.temp }}/f0-container/receipt.json" in workflow
    assert '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/ci.yml"' in workflow
    assert "name: f0-container-security" in workflow
    assert workflow.index("name: guided app starts") < workflow.index(
        "name: Scan actual image for high/critical known vulnerabilities"
    )


def test_f0_debian_openssl_hotfix_comes_from_pinned_security_snapshot():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "snapshot.debian.org/archive/debian-security/20261026T000000Z" in dockerfile
    assert "bookworm-security main" in dockerfile
    assert "libssl3=3.0.22-1~deb12u2" in dockerfile
    assert "openssl=3.0.22-1~deb12u2" in dockerfile
    assert "libseccomp2=2.5.4-1+deb12u1" in dockerfile
