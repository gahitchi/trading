"""Point-in-time universe: late joiners, delistings, weights over the active stocks."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from lab import backtest, data
from lab.config import RiskSettings
from lab.signals import BuyAndHold


def _frame(n, start="2024-01-02", px=100.0):
    idx = pd.bdate_range(start, periods=n)
    c = px * np.exp(np.linspace(0, 0.05, n))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e7}, index=idx)


def test_delisting_exit_and_late_joiner(goog_lab):
    frames = {"A": _frame(60), "B": _frame(30), "C": _frame(60)}  # B's data stops: delisted
    # B's known-in-advance index removal happens early enough for a real exit.
    # Its price-series endpoint alone is NOT accepted as an advance trading signal.
    b_exit = frames["B"].index[-3].date()
    membership = {
        "A": [(date(2024, 1, 1), None)],
        "B": [(date(2024, 1, 1), b_exit)],
        "C": [(date(2024, 2, 1), None)],
    }
    cfg = dataclasses.replace(goog_lab, warmup_start=date(2024, 1, 1))
    r = backtest.run(
        cfg,
        backtest.equal_weight(BuyAndHold, ["A", "B", "C"]),
        frames,
        date(2024, 1, 2),
        None,
        risk=RiskSettings(1.0, 1.0, 0.9999),
        membership=membership,
        normalize_active=True,
    )
    fills = pd.DataFrame([f.__dict__ for f in r.fills])
    fills["day"] = pd.to_datetime(fills.ts, utc=True).dt.tz_convert("America/New_York").dt.date
    b = fills[fills.ticker == "B"]
    assert b.side.tolist()[0] == "BUY" and b.side.tolist()[-1] == "SELL"
    assert b[b.side == "SELL"].qty.sum() == b[b.side == "BUY"].qty.sum()  # fully out of the delisted stock
    assert b.day.max() == frames["B"].index[-1].date()  # sold at its last real open
    c_first = fills[fills.ticker == "C"].day.min()
    assert c_first > date(2024, 2, 1)  # never held before it joined the universe
    assert r.ledger_vs_account == pytest.approx(0.0, abs=1e-6)
    assert r.gross.iloc[-1] > 0.9  # capital redeployed into the remaining members


def test_short_history_cannot_be_used_as_a_future_delisting_oracle():
    frames = {"A": _frame(60), "B": _frame(30)}
    with pytest.raises(ValueError, match="cannot infer a prescient liquidation"):
        backtest.active_windows(frames, None)
    with pytest.raises(ValueError, match="cannot infer a prescient liquidation"):
        backtest.active_windows(frames, {"B": [(date(2024, 1, 1), None)]})
    with pytest.raises(ValueError, match="cannot infer a prescient liquidation"):
        backtest.active_windows(frames, {"B": [(date(2024, 1, 1), frames["B"].index[-1].date())]})


def test_explicit_point_in_time_exit_is_not_derived_from_file_endpoint():
    frames = {"A": _frame(60), "B": _frame(30)}
    exit_day = frames["B"].index[-4].date()
    w = backtest.active_windows(frames, {"A": [(date(2024, 1, 1), None)], "B": [(date(2024, 1, 1), exit_day)]})
    assert w["B"][0][1] == backtest._ns_close(exit_day)


def test_membership_file_roundtrip(goog_lab, tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("ticker,start,end\naapl,2010-01-01,\nenron,1995-01-01,2001-12-02\nenron,2003-01-01,2003-06-01\n")
    data.save_membership(goog_lab, p)
    m = data.membership(goog_lab)
    assert m["AAPL"] == [(date(2010, 1, 1), None)] and len(m["ENRON"]) == 2
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker,start\nX,not-a-date\n")
    with pytest.raises(data.DataError, match="invalid start"):
        data.save_membership(goog_lab, bad)


def test_quality_flags_follow_the_data(goog_lab):
    q = data.quality(goog_lab, ["GOOG"])
    assert not q["survivorship_free"] and not q["adjusted"]
    assert "survivorship bias" in data.quality_warning(q)
