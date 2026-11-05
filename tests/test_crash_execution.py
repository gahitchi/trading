"""Executed-notional fee accounting and fixed-benchmark cost sensitivity.

Pure synthetic tests. No trading, downloaded data or protected holdout.
"""

import numpy as np
import pandas as pd
import pytest

from lab import crash_execution as execution
from lab import crash_study as crash


@pytest.mark.parametrize("side", [1, -1])
def test_reference_and_fixed_base_executed_notional_fees(side):
    entry, exit_ = 100.0, 90.0
    account = execution.fill_accounting(side, entry, exit_, 5.0, 0.0)
    assert account["entry_fill"] == pytest.approx(entry)
    assert account["exit_fill"] == pytest.approx(exit_)
    assert account["reference_gross"] == pytest.approx(side * (exit_ / entry - 1))
    assert account["fee_cost"] == pytest.approx(0.0005 * (100 + 90) / 100)
    assert account["slippage_cost"] == pytest.approx(0)
    assert account["net"] == pytest.approx(account["reference_gross"] - 0.00095)


@pytest.mark.parametrize("side", [1, -1])
def test_slippage_is_adverse_on_both_sides_and_fee_on_executed_prices(side):
    entry, exit_ = 100.0, 110.0
    a = execution.fill_accounting(side, entry, exit_, 5.0, 10.0)
    slip = 0.001
    entry_fill = entry * (1 + side * slip)
    exit_fill = exit_ * (1 - side * slip)
    assert a["entry_fill"] == pytest.approx(entry_fill)
    assert a["exit_fill"] == pytest.approx(exit_fill)
    assert a["slippage_cost"] == pytest.approx(slip * (entry + exit_) / entry)
    assert a["fee_cost"] == pytest.approx(0.0005 * (entry_fill + exit_fill) / entry)
    assert a["net"] == pytest.approx(side * (exit_fill - entry_fill) / entry - a["fee_cost"])


@pytest.mark.parametrize("side", [1, -1])
def test_more_slippage_never_improves_trade_return(side):
    observed = [execution.fill_accounting(side, 100.0, 103.0, 5.0, slip)["net"] for slip in (0.0, 5.0, 10.0, 20.0)]
    assert observed == sorted(observed, reverse=True)


@pytest.mark.parametrize(
    ("side", "entry", "exit_", "fee", "slip"),
    [
        (0, 100, 100, 5, 5),
        (True, 100, 100, 5, 5),
        (1, 0, 100, 5, 5),
        (-1, 100, 0, 5, 5),
        (1, 100, float("nan"), 5, 5),
        (1, 100, 100, float("inf"), 5),
        (-1, 100, 100, 5, -1),
        (-1, 100, 100, 5, 10000),
    ],
)
def test_execution_rejects_invalid_parameters(side, entry, exit_, fee, slip):
    with pytest.raises(ValueError):
        execution.fill_accounting(side, entry, exit_, fee, slip)


@pytest.mark.parametrize("field", ["fee_bps", "slippage_bps", "vol_target", "oi_pct", "drop_pct"])
def test_crash_settings_reject_nonfinite_economics(field):
    with pytest.raises(ValueError, match="finite"):
        crash.CrashSettings(**{field: float("nan")})


def test_trade_event_reference_prices_and_default_net_are_consistent():
    idx = pd.date_range("2024-02-01", periods=8, freq="min", tz="UTC")
    prices = [100.0, 100.0, 99.0, 98.0, 97.0, 95.0, 95.0, 95.0]
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": np.asarray(prices) + 0.2,
            "low": np.asarray(prices) - 0.2,
            "close": prices,
        },
        index=idx,
    )
    settings = crash.CrashSettings(
        mode="breakdown",
        window_min=1,
        hold_min=3,
        vol_target=0,
        delay_min=0,
        sl_pct=0,
        tp_pct=0,
        fee_bps=5,
        slippage_bps=5,
    )
    events = np.zeros(len(frame), dtype=bool)
    events[0] = True
    trades = crash.trades(frame, settings, events)
    assert len(trades) == 1
    trade = trades.iloc[0]
    assert trade["entry_reference"] == pytest.approx(100.0)
    assert trade["exit_reference"] == pytest.approx(98.0)
    direct = execution.fill_accounting(-1, 100, 98, 5, 5)
    assert trade["gross"] == pytest.approx(direct["reference_gross"])
    assert trade["net"] == pytest.approx(direct["net"])
    assert trade["fee_cost"] == pytest.approx(direct["fee_cost"])
    assert trade["slippage_cost"] == pytest.approx(direct["slippage_cost"])


def test_stress_preserves_trade_events_and_only_changes_cost_assumptions():
    idx = pd.date_range("2024-02-01", periods=4, freq="min", tz="UTC")
    t = pd.DataFrame(
        {
            "signal": [idx[0]],
            "entry": [idx[1]],
            "exit": [idx[3]],
            "entry_reference": [100.0],
            "exit_reference": [95.0],
            "gross": [0.05],
            "net": [0.04],
            "weight": [1.0],
            "reason": ["time"],
        }
    )
    original = t.copy(deep=True)
    base = execution.rescore(t, -1, 5.0, 5.0)
    harsh = execution.rescore(t, -1, 5.0, 20.0)
    assert t.equals(original)
    assert base["signal"].equals(harsh["signal"])
    assert base["entry"].equals(harsh["entry"])
    assert base["exit"].equals(harsh["exit"])
    assert base["weight"].equals(harsh["weight"])
    assert harsh["net"].iloc[0] < base["net"].iloc[0]
    scenarios = execution.stress_summary({"BTCUSDT": t}, -1, idx[0], idx[-1], crash.summary)
    assert [x["flat_price_round_trip_bps"] for x in scenarios] == [20, 30, 50]
    assert all(x["trades"] == 1 for x in scenarios)
    assert [x["mean_net_bps"] for x in scenarios] == sorted([x["mean_net_bps"] for x in scenarios], reverse=True)


def test_stress_fails_closed_on_inconsistent_trade_return():
    trade = pd.DataFrame(
        {
            "entry_reference": [100.0],
            "exit_reference": [90.0],
            "gross": [-0.9],
            "net": [-1.0],
        }
    )
    with pytest.raises(ValueError, match="disagree"):
        execution.rescore(trade, -1, 5.0, 5.0)


def test_stress_fails_closed_on_missing_trade_evidence():
    with pytest.raises(ValueError, match="reference price evidence"):
        execution.rescore(pd.DataFrame({"gross": [0.0], "net": [-0.002]}), -1, 5.0, 5.0)
