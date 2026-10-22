"""Fail-closed checkpoint and sealed-holdout regression tests."""

from types import SimpleNamespace

import pytest

from lab.cli import _period
from lab.config import Period
from lab.persistence import StateError, atomic_json_write, read_risk_checkpoint, risk_checkpoint


def test_risk_checkpoint_round_trip(tmp_path):
    path = tmp_path / "risk.json"
    atomic_json_write(path, risk_checkpoint(101_000, True, "drawdown limit"))
    state = read_risk_checkpoint(path)
    assert state["peak"] == 101_000
    assert state["halted"] is True
    assert state["reason"] == "drawdown limit"


@pytest.mark.parametrize("content", ["{", '{"version":1,"peak":NaN,"halted":false}', '{"peak":100}'])
def test_corrupt_checkpoint_fails_closed(tmp_path, content):
    path = tmp_path / "risk.json"
    path.write_text(content)
    with pytest.raises(StateError):
        read_risk_checkpoint(path)


def test_new_checkpoint_is_unhalted(tmp_path):
    assert read_risk_checkpoint(tmp_path / "new.json")["halted"] is False


def test_holdout_cannot_be_custom_period():
    cfg = SimpleNamespace(
        period=lambda name: Period(
            name, __import__("datetime").date(2024, 1, 1), __import__("datetime").date(2025, 1, 1)
        )
    )
    for args in [
        SimpleNamespace(period="holdout", start="2020-01-01", end="2020-12-01"),
        SimpleNamespace(period="train", start="2023-01-01", end=None),
        SimpleNamespace(period="train", start="2024-01-01", end="2025-01-01"),
    ]:
        with pytest.raises(SystemExit):
            _period(cfg, args)
