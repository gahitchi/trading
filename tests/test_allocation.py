"""Risk-based capital sharing ([portfolio] weighting): inverse volatility and Hierarchical Risk Parity."""

from dataclasses import replace

import numpy as np
import pytest

from lab import allocation, backtest, data
from lab.config import RiskSettings
from lab.signals.builtin import BUILTIN


def _closes(rets: np.ndarray, start=0):
    """Rows of (ts, price) per stock from a stocks x intervals return matrix."""
    out = {}
    for i, r in enumerate(rets):
        prices = 100 * np.exp(np.concatenate([[0.0], np.cumsum(r)]))
        out[f"S{i}"] = [(start + k, float(p)) for k, p in enumerate(prices)]
    return out


def test_inverse_vol_gives_each_stock_a_similar_risk():
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.01, (2, 400))
    rets = base * np.array([[1.0], [2.0]])  # the second stock is twice as volatile
    w = allocation.risk_weights("inverse_vol", _closes(rets), ["S0", "S1"], 20)
    assert w["S0"] == pytest.approx(2 / 3, abs=0.05) and sum(w.values()) == pytest.approx(1)


def test_hrp_gives_correlated_stocks_one_shared_budget():
    rng = np.random.default_rng(2)
    common, other = rng.normal(0, 0.01, 400), rng.normal(0, 0.01, 400)
    twin = common + rng.normal(0, 0.001, 400)
    rets = np.vstack([common, twin, other])  # S0 and S1 move together, S2 independently, similar volatility
    w = allocation.risk_weights("hrp", _closes(rets), ["S0", "S1", "S2"], 20)
    assert w["S2"] == pytest.approx(0.5, abs=0.05)
    assert w["S0"] == pytest.approx(w["S1"], abs=0.05) and sum(w.values()) == pytest.approx(1)
    equal_risk = allocation.risk_weights("inverse_vol", _closes(rets), ["S0", "S1", "S2"], 20)
    assert equal_risk["S2"] < 0.4  # inverse volatility alone would give the twins two full budgets


def test_too_little_shared_history_falls_back_to_equal():
    rets = np.random.default_rng(3).normal(0, 0.01, (2, 10))
    w = allocation.risk_weights("hrp", _closes(rets), ["S0", "S1"], 20)
    assert w == {"S0": 0.5, "S1": 0.5}


def test_only_intervals_both_stocks_share_are_used():
    closes = {"A": [(0, 100.0), (1, 101.0), (2, 102.0)], "B": [(0, 50.0), (2, 51.0)]}
    # A's intervals are (0,1) and (1,2), B's is (0,2): no common interval, so nothing is mixed.
    assert allocation.matched_returns(closes, ["A", "B"]) is None


def test_unknown_method_is_refused():
    with pytest.raises(ValueError, match="unknown weighting"):
        allocation.risk_weights("magic", {}, ["A"], 20)


def test_config_accepts_only_known_methods(goog_lab):
    from lab.config import ConfigError, load_config

    path = goog_lab.root / "lab.toml"
    text = path.read_text()
    path.write_text(text.replace('weighting = "equal"', 'weighting = "hrp"'))
    assert load_config(path).portfolio.weighting == "hrp"
    path.write_text(text.replace('weighting = "equal"', 'weighting = "max_sharpe"'))
    with pytest.raises(ConfigError, match="weighting"):
        load_config(path)


def test_inverse_vol_backtest_holds_more_of_the_calm_stock(goog_lab):
    calm = data.gbm(400, seed=5, ann_vol=0.10)
    wild = data.gbm(400, seed=6, ann_vol=0.40)
    frames = {"CALM": calm, "WILD": wild}
    sleeves = [backtest.Sleeve(BUILTIN["buy_and_hold"](), t, 0.5) for t in frames]
    start, end = calm.index[100].date(), calm.index[-1].date()

    def held_value(weighting):
        cfg = replace(goog_lab, portfolio=replace(goog_lab.portfolio, weighting=weighting))
        risk = RiskSettings(1.0, 1.0, 0.99)  # no per-stock cap, so the weighting alone decides the split
        r = backtest.run(cfg, sleeves, frames, start, end, normalize_active=True, risk=risk)
        qty = {t: 0.0 for t in frames}
        for f in r.fills:
            qty[f.ticker] += f.qty if f.side == "BUY" else -f.qty
        return {t: qty[t] * frames[t].close.iloc[-1] for t in frames}, r

    equal_hold, _ = held_value("equal")
    risk_hold, r = held_value("inverse_vol")
    assert r.signal_errors == 0
    ratio_equal = equal_hold["CALM"] / equal_hold["WILD"]
    ratio_risk = risk_hold["CALM"] / risk_hold["WILD"]
    assert ratio_risk > 2 * ratio_equal  # about 4x the volatility gap, damped by price drift since entry


@pytest.mark.parametrize("window, ok", [(2, False), (19, False), (20, True)])
def test_risk_weighting_needs_twenty_returns_of_history(goog_lab, window, ok):
    """ChatGPT C005-F4: with vol_window 2 the allocator used HRP on two returns despite the stated warm-up."""
    from lab.config import ConfigError, load_config

    path = goog_lab.root / "lab.toml"
    text = path.read_text().replace('weighting = "equal"', 'weighting = "hrp"')
    path.write_text(text.replace("vol_window = 63", f"vol_window = {window}"))
    if ok:
        assert load_config(path).portfolio.vol_window == 20
    else:
        with pytest.raises(ConfigError, match="vol_window >= 20"):
            load_config(path)


def test_the_allocator_demands_twenty_shared_returns_whatever_its_window():
    from lab.agents.allocator import AllocatorAgent, AllocatorAgentConfig

    rng = np.random.default_rng(4)
    closes = _closes(rng.normal(0, 0.01, (2, 10)) * np.array([[1.0], [3.0]]))
    agent = AllocatorAgent(
        AllocatorAgentConfig(sleeves={"s:S0": ("S0", 0.5), "s:S1": ("S1", 0.5)}, vol_window=2, weighting="hrp")
    )
    from collections import deque

    agent.closes = {t: deque(rows) for t, rows in closes.items()}
    agent.targets = {"s:S0": 1.0, "s:S1": 1.0}
    assert agent.weights() == {"S0": 0.5, "S1": 0.5}  # ten returns: still equal
