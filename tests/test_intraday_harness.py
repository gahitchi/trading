"""A quick-speed intraday session through the real paper node (C003), compressed into seconds."""

import itertools
import re

import numpy as np
import pandas as pd
import pytest

from lab import intraday
from lab.config import load_config
from lab.halt import read_halt
from lab.signals.registry import SignalSpec
from lab.sim.intraday import IntradayHarness, harness_config

# Real-clock LiveNode runs against the simulated broker: the slow, serial tier (Gemini audit F-R19).
pytestmark = pytest.mark.harness


def _frames(minutes=15, sessions=3):
    idx = [t for d in pd.bdate_range("2026-03-02", periods=sessions) for t in intraday.session_bars(d, minutes)]
    o = 100 + 0.1 * np.arange(len(idx))
    df = pd.DataFrame(
        {"open": o, "high": o + 0.2, "low": o - 0.2, "close": o + 0.05, "volume": 1e6}, index=pd.DatetimeIndex(idx)
    )
    return {"GOOG": df}


def _cfg(goog_lab, **trading):
    path = goog_lab.root / "lab.toml"
    path.write_text(re.sub(r'(?m)^profile = "\w+"', 'profile = "aggressive"', path.read_text()))
    return harness_config(load_config(path), "15m", **trading)


def test_a_compressed_15m_session_enters_with_a_limit_and_is_flat_at_the_close(goog_lab, tmp_path):
    cfg = _cfg(goog_lab)
    h = IntradayHarness(cfg, [SignalSpec("buy_and_hold")], _frames(), tmp_path / "h", first_day=2)
    r = h.session()
    assert r.error is None, r.error
    orders = list(h.broker().state["orders"].values())
    kinds = [e["kind"] for e in r.risk_events]
    assert read_halt(h.runtime / "HALT") is None, kinds
    buys = [o for o in orders if o["side"] == "BUY" and o["status"] == "FILLED"]
    sells = [o for o in orders if o["side"] == "SELL" and o["status"] == "FILLED"]
    assert buys and buys[0]["type"] == "LIMIT", orders  # new exposure: a collared limit
    assert sells and sells[-1]["type"] == "MARKET", orders  # the exit at the close must go: a market order
    assert "flat_at_close" in kinds
    assert r.broker_positions == {} and not any(r.ledger["positions"].values())
    assert r.ledger["cash"] == pytest.approx(r.broker_cash, abs=0.01)  # the ledger matches the broker exactly
    assert len(r.fills) >= 2 and r.fills[-1].side == "SELL"


def test_a_limit_the_market_runs_away_from_is_cancelled_a_bar_later_and_never_chased(goog_lab, tmp_path):
    frames = _frames()
    df = frames["GOOG"]
    day = df.index.normalize() == df.index.normalize()[-1]
    # On the replay day every bar opens 3% above the last: a 1% collar computed from delayed data never crosses.
    n = int(day.sum())
    jump = 100 * 1.03 ** np.arange(n)
    df.loc[day, ["open", "high", "low", "close"]] = np.column_stack([jump, jump * 1.001, jump * 0.999, jump])
    h = IntradayHarness(_cfg(goog_lab), [SignalSpec("buy_and_hold")], frames, tmp_path / "h", first_day=2)
    r = h.session()
    assert r.error is None, r.error
    orders = list(h.broker().state["orders"].values())
    assert orders and all(o["type"] == "LIMIT" and o["filled"] == 0 for o in orders)
    cancelled = [o for o in orders if o["status"] == "CANCELED" and o["reason"] is None]  # by the agent
    assert len(cancelled) >= 2  # each re-priced from fresh data, one at a time
    by_time = sorted(orders, key=lambda o: o["ts_accepted"])
    # One at a time: each order is sent only after the previous one was cancelled.
    assert all(b["ts_accepted"] >= a["ts_last"] for a, b in itertools.pairwise(by_time))
    assert r.broker_positions == {} and read_halt(h.runtime / "HALT") is None
