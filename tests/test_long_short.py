"""Signed (long/short) books, leverage and margin, through the real agents in the backtest engine."""

import itertools
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from lab import backtest, data, margin
from lab.agents.risk import RiskAgent, RiskAgentConfig
from lab.config import ConfigError, MarginSettings, RiskSettings, _check_risk
from lab.signals.base import Signal, Window
from lab.signals.registry import SignalSpec

M = MarginSettings()


class Constant(Signal):
    name = "constant"
    lookback = 1
    defaults = {"x": -1.0}

    def target(self, w: Window) -> float:
        return self.params["x"]


def _risk(**kw):
    base = {
        "max_weight": 1.0,
        "max_gross": 1.0,
        "max_drawdown": 0.99,
        "max_short_gross": 1.0,
        "min_net": -1.0,
        "max_net": 1.0,
    }
    return RiskSettings(**{**base, **kw})


def _agent(**kw):
    from nautilus_trader.model import ActorId

    r = _risk(**kw)
    return RiskAgent(
        RiskAgentConfig(
            actor_id=ActorId("R"),
            max_weight=r.max_weight,
            max_gross=r.max_gross,
            max_drawdown=r.max_drawdown,
            max_short_gross=r.max_short_gross,
            min_net=r.min_net,
            max_net=r.max_net,
            margin=M,
        )
    )


def _run(cfg, signal, frames, start, end, risk, **pf):
    cfg = replace(cfg, risk=risk, portfolio=replace(cfg.portfolio, **pf))
    t = next(iter(frames))
    return backtest.run(cfg, [backtest.Sleeve(signal, t, 1.0)], frames, start, end, capital=100_000)


def _positions(fills):
    pos, path = 0.0, []
    for f in fills:
        pos += f.qty if f.side == "BUY" else -f.qty
        path.append(pos)
    return path


# ---------------------------------------------------------------------------- limits (fast)


def test_config_refuses_more_leverage_than_the_broker_allows():
    _check_risk(_risk(max_gross=2.0), M)  # Reg T: 50% initial -> 2x is the ceiling
    with pytest.raises(ConfigError, match="initial margin"):
        _check_risk(_risk(max_gross=2.5), M)
    with pytest.raises(ConfigError, match="max_short_gross"):
        _check_risk(_risk(max_short_gross=1.5), M)
    with pytest.raises(ConfigError, match="min_net"):
        _check_risk(_risk(max_short_gross=0.0, min_net=-0.5), M)


def test_risk_agent_signed_limits():
    a = _agent(max_weight=0.5, max_gross=1.6, max_short_gross=0.6, min_net=-0.2, max_net=1.0)
    w = a._approve({"A": 0.9, "B": 0.8, "C": -0.7, "D": -0.5}, 0).weights
    assert w == pytest.approx({"A": 0.5, "B": 0.5, "C": -0.3, "D": -0.3})  # per-name caps, short book scaled
    # Net above the limit trims the longs; below it trims the shorts.
    w = _agent(max_net=0.2)._approve({"A": 0.6, "B": -0.2}, 0).weights
    assert w["A"] - 0.2 == pytest.approx(0.2) and w["B"] == -0.2
    w = _agent(min_net=-0.1)._approve({"A": 0.1, "B": -0.6}, 0).weights
    assert w["A"] + w["B"] == pytest.approx(-0.1)
    # Long-only configuration: shorts never pass.
    assert _agent(max_short_gross=0.0, min_net=0.0)._approve({"A": -0.5, "B": 0.3}, 0).weights == {"A": 0.0, "B": 0.3}
    # Initial margin with the cushion: at most 1/1.1 of equity as Reg T requirement (gross ~1.82x).
    w = _agent(max_gross=2.0, max_short_gross=2.0, min_net=-2.0, max_net=2.0)._approve({"A": 1.0, "B": -1.0}, 0)
    assert margin.initial_requirement(w.weights, M) == pytest.approx(1 / 1.1)


def test_margin_arithmetic():
    assert margin.maintenance_requirement(100, 50, M) == pytest.approx(25 + 15)
    assert margin.deleverage_scale(100, 100, 50, M) == 1.0
    assert margin.deleverage_scale(30, 100, 50, M) == pytest.approx(30 / 44)
    # Borrowed cash: a levered long (cash -50) borrows 50; a dollar-neutral book borrows nothing.
    assert margin.borrowed_cash(-50, 0) == 50 and margin.borrowed_cash(100, 50) == 0
    borrow, debit = margin.financing_cost(cash=-50, short_value=0, days=365, m=M)
    assert borrow == 0 and debit == pytest.approx(50 * M.debit_apr)


# ---------------------------------------------------------------------------- engine


def test_long_only_configuration_never_shorts_even_when_the_signal_wants_to(goog_lab, goog):
    r = _run(
        goog_lab,
        SignalSpec("sma_trend", {"fast": 3, "slow": 10, "short": 1}).make(),
        {"GOOG": goog},
        goog.index[300].date(),
        goog.index[500].date(),
        goog_lab.risk,
    )
    assert r.fills and min(_positions(r.fills)) >= 0 and not r.margin_account


def test_shorts_trade_in_a_margin_account_and_the_ledger_matches_it(goog_lab, goog):
    risk = _risk(max_weight=0.5, max_short_gross=0.5, min_net=-0.5)
    r = _run(
        goog_lab,
        SignalSpec("sma_trend", {"fast": 3, "slow": 10, "short": 1}).make(),
        {"GOOG": goog},
        goog.index[300].date(),
        goog.index[500].date(),
        risk,
    )
    path = _positions(r.fills)
    assert r.margin_account and min(path) < 0 < max(path)  # it went short and long
    assert r.ledger_vs_account == pytest.approx(0.0, abs=1e-6)  # cash identity of the margin account
    assert r.borrow_fees > 0 and r.financing >= r.borrow_fees
    assert r.signal_errors == 0 and r.rejections == 0


def test_borrow_fee_matches_the_short_book(goog_lab):
    flat = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e7},
        index=pd.bdate_range("2021-01-04", periods=260),
    )
    r = _run(goog_lab, Constant(x=-1.0), {"GOOG": flat}, flat.index[1].date(), None, _risk(max_weight=1.0))
    days = (flat.index[-1] - flat.index[2]).days  # short from the first fill to the end
    short_value = 0.99 * 100_000  # cash buffer 1%
    assert r.borrow_fees == pytest.approx(short_value * M.borrow_apr * days / 365, rel=0.03)
    assert r.financing == pytest.approx(r.borrow_fees)  # proceeds cover the short: nothing borrowed


def test_fixed_leverage_borrows_and_pays_debit_interest(goog_lab):
    flat = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e7},
        index=pd.bdate_range("2021-01-04", periods=260),
    )
    r = _run(
        goog_lab,
        Constant(x=1.0),
        {"GOOG": flat},
        flat.index[1].date(),
        None,
        _risk(max_weight=1.5, max_gross=1.5, max_short_gross=0.0, min_net=0.0, max_net=1.5),  # one stock
        leverage=1.5,
        max_leverage=1.5,
    )
    assert r.gross.iloc[-1] == pytest.approx(1.5 * 0.99, rel=0.01)
    assert r.min_cash < 0 and r.financing > 0 and r.borrow_fees == 0
    assert r.ledger_vs_account == pytest.approx(0.0, abs=1e-6)


def test_overnight_gap_on_a_levered_short_triggers_a_margin_call_and_only_reduces(goog_lab):
    n = 40
    close = np.full(n, 100.0)
    close[20:] = 130.0  # a 30% gap up against the short
    df = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e7},
        index=pd.bdate_range("2021-01-04", periods=n),
    )
    risk = _risk(max_weight=2.0, max_gross=2.0, max_short_gross=2.0, min_net=-2.0, max_net=2.0)  # one stock
    r = _run(goog_lab, Constant(x=-1.0), {"GOOG": df}, df.index[1].date(), None, risk, leverage=2.0, max_leverage=2.0)
    kinds = [e.kind for e in r.risk_events]
    assert "margin_call" in kinds and "deleverage" in kinds
    path = _positions(r.fills)
    first_call = next(i for i, f in enumerate(r.fills) if f.side == "BUY")
    after = [abs(p) for p in path[first_call:]]
    assert all(b <= a for a, b in itertools.pairwise(after))  # after the call, |short| only shrinks
    assert r.ledger_vs_account == pytest.approx(0.0, abs=1e-6)


def test_momentum_long_short_is_dollar_neutral():
    from lab.signals.builtin import MomentumLongShort

    rng = np.random.default_rng(3)
    windows = {}
    for i, t in enumerate("ABCDEFGH"):
        c = 100 * np.exp(np.cumsum(rng.normal(0.001 * (i - 4), 0.01, 253)))
        windows[t] = Window(c, c, c, c, np.ones(253), np.arange(253))
    w = MomentumLongShort(top=2, bottom=2).weights(windows)
    assert sum(v for v in w.values() if v > 0) == pytest.approx(0.5)
    assert sum(v for v in w.values() if v < 0) == pytest.approx(-0.5)
    assert len(w) == 4 and MomentumLongShort(top=2, bottom=2).weights(dict(list(windows.items())[:3])) == {}


def test_frames_fixture_is_real_data(goog):
    assert len(data.validate(goog, "GOOG")) == 2148
