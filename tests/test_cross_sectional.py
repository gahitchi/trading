"""Cross-sectional (ranking) strategies."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from lab import backtest
from lab.config import RiskSettings
from lab.signals import Momentum, Window
from lab.signals.base import clean_weights


def _window(closes):
    c = np.asarray(closes, dtype=float)
    return Window(c, c, c, c, np.ones_like(c))


def test_momentum_ranks_by_twelve_minus_one_return():
    m = Momentum(top=2)
    n = m.lookback
    up_fast = np.linspace(100, 200, n)
    up_slow = np.linspace(100, 120, n)
    down = np.linspace(100, 80, n)
    late_spike = np.r_[np.full(n - 10, 100.0), np.full(10, 500.0)]  # only the excluded last month moved
    w = m.weights({"A": _window(up_fast), "B": _window(up_slow), "C": _window(down), "D": _window(late_spike)})
    assert w == {"A": 0.5, "B": 0.5}


def test_clean_weights_keeps_signs_and_caps_gross_at_the_strategy_capital():
    # Negative = short; whether it is traded is the risk limits' decision. |weights| sum to at most 1.
    assert clean_weights({"A": 0.8, "B": 0.6, "C": -1, "D": float("nan")}) == pytest.approx(
        {"A": 0.8 / 2.4, "B": 0.6 / 2.4, "C": -1 / 2.4}
    )
    assert clean_weights({"A": 0.3, "B": -0.2}) == {"A": 0.3, "B": -0.2}
    assert clean_weights({"A": -3}) == {"A": -1.0}  # clamped to [-1, 1] first
    assert clean_weights(None) == {}


def test_momentum_portfolio_end_to_end(goog_lab):
    idx = pd.bdate_range("2020-01-02", periods=700)
    frames = {}
    for t, mu in (("W", 0.0008), ("X", 0.0004), ("Y", 0.0), ("Z", -0.0008)):
        c = 100 * np.exp(mu * np.arange(len(idx)))  # noise-free trends: the ranking is unambiguous
        frames[t] = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e7}, index=idx)
    cfg = dataclasses.replace(goog_lab, warmup_start=date(2020, 1, 1))
    r = backtest.run(
        cfg,
        [],
        frames,
        date(2021, 3, 1),
        None,
        risk=RiskSettings(1.0, 1.0, 0.9999),
        cross_sectional=[(Momentum(top=2), 1.0, list(frames))],
    )
    assert r.signal_errors == 0 and r.ledger_vs_account == pytest.approx(0.0, abs=1e-6)
    assert {f.ticker for f in r.fills} == {"W", "X"}  # the top two; flat Y and falling Z never qualify
    assert r.metrics["avg_exposure"] > 0.8


def test_paper_node_builds_with_ranking_strategy(goog_lab):
    from lab.paper import build_node
    from lab.signals.registry import SignalSpec

    cfg = dataclasses.replace(goog_lab, paper=dataclasses.replace(goog_lab.paper, account_id="DU1234567"))
    node = build_node(cfg, [SignalSpec("momentum", {"top": 1}), SignalSpec("sma_trend")], ["GOOG"])
    node.dispose()


def test_volatility_targeting_on_real_prices(goog_lab, goog):
    plain = backtest.run(
        goog_lab,
        backtest.equal_weight(lambda: __import__("lab.signals", fromlist=["x"]).BuyAndHold(), ["GOOG"]),
        {"GOOG": goog},
        date(2006, 1, 1),
        date(2010, 12, 31),
        risk=RiskSettings(1.0, 1.0, 0.9999),
    )
    targeted = backtest.run(
        goog_lab,
        backtest.equal_weight(lambda: __import__("lab.signals", fromlist=["x"]).BuyAndHold(), ["GOOG"]),
        {"GOOG": goog},
        date(2006, 1, 1),
        date(2010, 12, 31),
        risk=RiskSettings(1.0, 1.0, 0.9999),
        target_vol=0.15,
    )
    assert plain.metrics["volatility"] > 0.30  # GOOG alone: ~40% a year
    assert targeted.metrics["volatility"] < 0.6 * plain.metrics["volatility"]
    assert targeted.metrics["avg_exposure"] < 0.7
    assert targeted.ledger_vs_account == pytest.approx(0.0, abs=1e-6)
