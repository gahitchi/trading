"""Guard F0 build toolchain configuration against silently ignored Actions inputs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP_UV = re.compile(
    r'(?m)^\s*- uses: astral-sh/setup-uv@[0-9a-f]{40}\s*$\n'
    r'\s+with:\s*\n'
    r'\s+version: "[0-9]+\.[0-9]+\.[0-9]+"\s*$'
)


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".github/workflows/f0-two-runner-wheel.yml"])
def test_all_f0_uv_actions_pin_supported_version_input(path):
    workflow = (ROOT / path).read_text(encoding="utf-8")
    assert "uv-version:" not in workflow
    count = workflow.count("uses: astral-sh/setup-uv@")
    assert count >= 1
    assert len(SETUP_UV.findall(workflow)) == count
