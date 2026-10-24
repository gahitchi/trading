"""The trading layer: fill timing, accounting, look-ahead, risk limits."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from lab import backtest
from lab.config import RiskSettings
from lab.signals import BuyAndHold, RsiReversion, Signal, SmaTrend
from tests.conftest import staircase

NO_RISK = RiskSettings(1.0, 1.0, 0.9999)


def test_orders_fill_at_next_open_never_same_close(goog_lab):
    df = staircase()
    cfg = dataclasses.replace(goog_lab, warmup_start=date(2024, 1, 1))
    r = backtest.run(cfg, backtest.equal_weight(BuyAndHold, ["X"]), {"X": df}, date(2024, 1, 2), None, risk=NO_RISK)
    first = r.fills[0]
    # Decision at the close of 2024-01-02 (105) -> fill at 2024-01-03's open (110) + one tick of slippage.
    assert first.side == "BUY"
    assert first.price == pytest.approx(110.01)
    assert (
        pd.Timestamp(first.ts, tz="UTC").tz_convert("America/New_York").strftime("%Y-%m-%d %H:%M") == "2024-01-03 09:30"
    )


@pytest.mark.parametrize("sig", [BuyAndHold, SmaTrend, RsiReversion])
def test_lab_ledger_matches_simulator_account(goog_lab, goog, sig):
    r = backtest.run(
        goog_lab,
        backtest.equal_weight(sig, ["GOOG"]),
        {"GOOG": goog},
        date(2006, 1, 1),
        date(2010, 12, 31),
        risk=NO_RISK,
    )
    assert len(r.fills) > 0
    assert r.ledger_vs_account == pytest.approx(0.0, abs=1e-6)
    assert r.signal_errors == 0 and r.rejections == 0


def test_buy_and_hold_tracks_the_stock(goog_lab, goog):
    r = backtest.run(
        goog_lab,
        backtest.equal_weight(BuyAndHold, ["GOOG"]),
        {"GOOG": goog},
        date(2006, 1, 1),
        date(2010, 12, 31),
        risk=NO_RISK,
    )
    w = goog.loc["2006-01-04":"2010-12-31"]
    stock = w.close.iloc[-1] / w.open.iloc[0] - 1
    assert r.metrics["total_return"] == pytest.approx(stock * 0.99, abs=0.03)  # 99% invested, costs
    assert r.metrics["avg_exposure"] > 0.95


class Spy(Signal):
    """Records what it is shown, to prove the window holds only the past."""

    name = "spy"
    lookback = 5
    defaults = {}
    seen: list = []

    def target(self, w):
        Spy.seen.append((len(w), float(w.close[-1]), str(w.dates[-1])))
        return 1.0


def test_signals_see_exactly_lookback_past_bars(goog_lab):
    df = staircase(20)
    Spy.seen = []
    cfg = dataclasses.replace(goog_lab, warmup_start=date(2024, 1, 1))
    backtest.run(cfg, [backtest.Sleeve(Spy(), "X", 1.0)], {"X": df}, date(2024, 1, 2), None, risk=NO_RISK)
    closes = list(df.close)
    assert [n for n, _, _ in Spy.seen] == [5] * 16
    assert [c for _, c, _ in Spy.seen] == closes[4:]  # the newest bar in the window is the one that just closed
    assert [d for _, _, d in Spy.seen] == [str(x.date()) for x in df.index[4:]]  # with its session date


def test_window_is_read_only():
    from lab.signals.base import Window

    w = Window([1.0], [1.0], [1.0], [1.0], [1.0])
    with pytest.raises(ValueError):
        w.close[0] = 2.0
    with pytest.raises(AttributeError):
        w.close = np.array([3.0])


def test_risk_agent_caps_each_stock(goog_lab, goog):
    data = {"A": goog, "B": goog.copy()}
    risk = RiskSettings(0.25, 1.0, 0.9999)
    sleeves = [backtest.Sleeve(BuyAndHold(), "A", 0.5), backtest.Sleeve(BuyAndHold(), "B", 0.5)]
    r = backtest.run(goog_lab, sleeves, data, date(2006, 1, 1), date(2006, 12, 31), risk=risk)
    assert r.metrics["avg_exposure"] == pytest.approx(0.5, abs=0.05)  # 2 x 25% cap


def test_circuit_breaker_flattens_and_halts(goog_lab):
    idx = pd.bdate_range("2024-01-02", periods=60)
    close = np.concatenate([np.full(10, 100.0), np.linspace(100, 50, 20), np.linspace(50, 120, 30)])
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1e7}, index=idx)
    cfg = dataclasses.replace(goog_lab, warmup_start=date(2024, 1, 1))
    r = backtest.run(
        cfg,
        backtest.equal_weight(BuyAndHold, ["X"]),
        {"X": df},
        date(2024, 1, 2),
        None,
        risk=RiskSettings(1.0, 1.0, 0.2),
    )
    breakers = [e for e in r.risk_events if e.kind == "circuit_breaker"]
    assert len(breakers) == 1
    assert {e.kind for e in r.risk_events} <= {"circuit_breaker", "reservation_blocked"}
    assert r.gross.iloc[-1] == 0.0  # stayed flat through the recovery
    assert r.fills[0].side == "BUY" and r.fills[-1].side == "SELL"
    assert all(f.side == "SELL" for f in r.fills if f.ts > breakers[0].ts)
    assert sum(f.qty if f.side == "BUY" else -f.qty for f in r.fills) == 0
