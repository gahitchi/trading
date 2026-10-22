"""Market analysis is an explicit read-only, pre-holdout workflow."""

import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lab import cli
from lab.market_analysis import MarketAnalysisError


def _args(**changed):
    values = {
        "tickers": ["X"],
        "as_of": None,
        "lookback": 252,
        "risk_window": 63,
        "momentum_days": 252,
        "skip_recent": 21,
        "shrinkage": 0.15,
        "output": None,
        "json": True,
    }
    values.update(changed)
    return SimpleNamespace(**values)


def _setup(monkeypatch):
    idx = pd.bdate_range("2023-01-02", periods=320)
    close = 100 * np.exp(0.001 * np.arange(len(idx)))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )
    cfg = SimpleNamespace(period=lambda name: SimpleNamespace(start=date(2025, 1, 1)))
    monkeypatch.setattr(cli, "_tickers", lambda cfg, given: ["X"])
    monkeypatch.setattr(cli.data, "load_many", lambda cfg, given: {"X": frame})
    monkeypatch.setattr(cli.data, "manifest", lambda cfg: {"X": {"source": "offline-fixture"}})
    monkeypatch.setattr(cli.data, "quality", lambda cfg, tickers: {"valid": True})
    monkeypatch.setattr(cli.data, "quality_warning", lambda quality: None)
    return cfg, frame


def test_cli_parses_a_read_only_analysis_command():
    args = cli.build_parser().parse_args(["analyze", "--tickers", "X", "--as-of", "2024-03-01", "--json"])
    assert args.fn is cli.cmd_analyze
    assert args.as_of == "2024-03-01"


def test_cli_emits_reproducible_json(monkeypatch, capsys):
    cfg, frame = _setup(monkeypatch)
    cli.cmd_analyze(cfg, _args())
    first = json.loads(capsys.readouterr().out)
    cli.cmd_analyze(cfg, _args())
    second = json.loads(capsys.readouterr().out)
    assert first == second
    assert first["as_of"] == str(frame.index[-1].date())
    assert first["qualification"] == "EXPLORATORY_ONLY"
    assert len(first["analysis_id"]) == 64


@pytest.mark.parametrize("cutoff", ["2025-01-01", "2030-01-01", "bad-date"])
def test_sealed_holdout_and_bad_dates_are_denied(monkeypatch, cutoff):
    cfg, _ = _setup(monkeypatch)
    with pytest.raises(MarketAnalysisError):
        cli.cmd_analyze(cfg, _args(as_of=cutoff))


def test_report_can_be_saved_once_but_never_silently_replaced(monkeypatch, tmp_path, capsys):
    cfg, _ = _setup(monkeypatch)
    output = tmp_path / "analysis.json"
    cli.cmd_analyze(cfg, _args(output=str(output)))
    assert json.loads(output.read_text())["qualification"] == "EXPLORATORY_ONLY"
    capsys.readouterr()
    with pytest.raises(MarketAnalysisError, match="refusing to overwrite"):
        cli.cmd_analyze(cfg, _args(output=str(output)))
