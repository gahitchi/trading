"""Config, data validation, costs, metrics, safety."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from lab import data
from lab.config import ConfigError, load_config
from lab.costs import ibkr_fixed_commission
from lab.metrics import max_drawdown, summarize
from lab.safety import SafetyError, check_paper_settings, is_paper_account
from tests.conftest import REPO, staircase


def test_repo_config_loads():
    cfg = load_config(REPO / "lab.toml")
    assert cfg.period("train").end < cfg.period("test").start < cfg.period("holdout").start
    assert "SPY" in cfg.universe and cfg.paper.port == 4002


def test_overlapping_periods_rejected(tmp_path):
    s = (REPO / "lab.toml").read_text().replace('test = ["2019-01-01"', 'test = ["2018-06-01"')
    (tmp_path / "lab.toml").write_text(s)
    with pytest.raises(ConfigError, match="overlap"):
        load_config(tmp_path / "lab.toml")


def test_validate_repairs_tiny_inconsistencies_and_rejects_corruption():
    df = staircase(5)
    df.loc[df.index[1], "high"] = df.close.iloc[1] * 0.999  # adjustment rounding
    assert (data.validate(df, "X").high >= data.validate(df, "X").close).all()
    df.loc[df.index[2], "high"] = df.close.iloc[2] * 0.9
    with pytest.raises(data.DataError, match="inconsistent"):
        data.validate(df, "X")


def test_validate_rejects_duplicates_and_bad_prices():
    df = staircase(5)
    with pytest.raises(data.DataError, match="duplicate"):
        data.validate(pd.concat([df, df.iloc[[0]]]), "X")
    bad = staircase(5)
    bad.loc[bad.index[0], "low"] = -1
    with pytest.raises(data.DataError, match="non-positive"):
        data.validate(bad, "X")


def test_block_bootstrap_keeps_dates_and_validity(goog):
    b = data.block_bootstrap(goog, seed=3)
    assert b.index.equals(goog.index)
    data.validate(b, "boot")
    assert not np.allclose(b.close.to_numpy(), goog.close.to_numpy())
    assert data.block_bootstrap(goog, seed=3).equals(b)  # reproducible


def test_storage_roundtrip_with_provenance(goog_lab):
    m = data.manifest(goog_lab)["GOOG"]
    assert m["rows"] == 2148 and m["source"] == "test"
    assert len(data.load(goog_lab, "GOOG")) == 2148


@pytest.mark.parametrize("shares,price,expected", [(10, 100, 1.0), (1000, 50, 5.0), (100, 0.5, 0.5)])
def test_ibkr_fixed_commission(shares, price, expected):
    assert ibkr_fixed_commission(shares, price) == pytest.approx(expected)


def test_metrics():
    eq = pd.Series([100, 120, 90, 95, 130], index=pd.bdate_range("2024-01-01", periods=5), dtype=float)
    mdd, days = max_drawdown(eq)
    assert mdd == pytest.approx(0.25) and days == 2
    m = summarize(eq)
    assert m["total_return"] == pytest.approx(0.30) and m["max_drawdown"] == pytest.approx(0.25)


@pytest.mark.parametrize(
    "port,acct,msg",
    [
        (4001, "DU1234567", "live port"),
        (7496, "DU1234567", "live port"),
        (4002, "", "account_id"),
        (4002, "U1234567", "not an IB paper"),
        (5555, "DU1234567", "not a known paper port"),
    ],
)
def test_paper_settings_guard(port, acct, msg):
    with pytest.raises(SafetyError, match=msg):
        check_paper_settings(port, acct)


def test_paper_settings_ok():
    check_paper_settings(4002, "DU1234567")
    check_paper_settings(7497, "DU1234567")
    assert is_paper_account("IB-DU1234567") and not is_paper_account("IB-U1234567")


def test_paper_node_builds_without_connecting(goog_lab):
    from lab.paper import build_node
    from lab.signals.registry import SignalSpec

    cfg = dataclasses.replace(goog_lab, paper=dataclasses.replace(goog_lab.paper, account_id="DU1234567"))
    node = build_node(cfg, [SignalSpec("sma_trend")], ["GOOG"])
    assert not node.is_running
    node.dispose()
