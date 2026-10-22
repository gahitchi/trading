"""The forecast CLI must never expose the sealed final holdout or place orders."""

import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lab import cli
from lab.market_analysis import MarketAnalysisError


def prepare(monkeypatch):
    idx = pd.bdate_range("2023-01-02", periods=650)
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
    config = SimpleNamespace(period=lambda name: SimpleNamespace(start=date(2025, 1, 1)))
    monkeypatch.setattr(cli, "_tickers", lambda cfg, tickers: ["X"])
    monkeypatch.setattr(cli.data, "load_many_unmodified", lambda cfg, tickers: {"X": frame})
    monkeypatch.setattr(cli.data, "manifest", lambda cfg: {"X": {"source": "synthetic fixture"}})
    monkeypatch.setattr(cli.data, "quality", lambda cfg, tickers: {})
    monkeypatch.setattr(cli.data, "quality_warning", lambda quality: None)
    return config


def args(**overrides):
    values = {
        "ticker": "X",
        "as_of": None,
        "horizon": 1,
        "min_train": 160,
        "calibration": 45,
        "test_sessions": 45,
        "ridge_alpha": 10.0,
        "coverage": 0.9,
        "drift_z_limit": 5.0,
        "output": None,
        "json": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_forecast_cli_parser():
    parsed = cli.build_parser().parse_args(["forecast", "--ticker", "X", "--horizon", "3"])
    assert parsed.fn is cli.cmd_forecast
    assert parsed.horizon == 3


def test_forecast_report_deterministic_and_read_only(monkeypatch, capsys):
    config = prepare(monkeypatch)
    cli.cmd_forecast(config, args())
    first = json.loads(capsys.readouterr().out)
    cli.cmd_forecast(config, args())
    second = json.loads(capsys.readouterr().out)
    assert first == second
    assert first["qualification"] == "RESEARCH_ONLY_NOT_FOR_TRADING"
    assert first["as_of"] < "2025-01-01"


@pytest.mark.parametrize("date_string", ["2025-01-01", "2040-01-01", "not-a-date"])
def test_sealed_holdout_never_accessible(monkeypatch, date_string):
    config = prepare(monkeypatch)
    with pytest.raises(MarketAnalysisError):
        cli.cmd_forecast(config, args(as_of=date_string))


def test_output_is_create_exclusive(monkeypatch, tmp_path, capsys):
    config = prepare(monkeypatch)
    dest = tmp_path / "forecast.json"
    cli.cmd_forecast(config, args(output=str(dest)))
    assert json.loads(dest.read_text())["qualification"] == "RESEARCH_ONLY_NOT_FOR_TRADING"
    capsys.readouterr()
    with pytest.raises(MarketAnalysisError, match="refusing to overwrite"):
        cli.cmd_forecast(config, args(output=str(dest)))
