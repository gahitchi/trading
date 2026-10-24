from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from lab.authority import digest
from lab.signals.registry import SignalSpec
from lab.strategy_bank import StrategyBank


def manifest():
    names = ["sma_trend", "rsi_reversion"]
    return {
        "version": 1,
        "selection": "fixed_priority_regime_v1",
        "benchmark": "A",
        "window": 20,
        "high_volatility": 0.3,
        "trend_band": 0.03,
        "entries": [
            {
                "name": n,
                "artifact": digest({"name": n, "params": {}, "sha256": None}),
                "priority": i,
                "regimes": ["up"] if i == 0 else ["down", "range"],
                "eligible_after": "2024-01-01T00:00:00+00:00",
                "expires_at": "2026-01-01T00:00:00+00:00",
            }
            for i, n in enumerate(names)
        ],
    }


def bank(m=None):
    return StrategyBank(m or manifest(), [SignalSpec("sma_trend"), SignalSpec("rsi_reversion")], ["A", "B"])


def history(rate=0.005, start=datetime(2025, 1, 1, tzinfo=UTC)):
    return [(int((start + timedelta(days=i)).timestamp() * 1e9), 100 * (1 + rate) ** i) for i in range(21)]


def test_regime_selection_retirement_and_cash_fallback():
    b = bank()
    assert b.select({"A": history()}) == ("sma_trend", "up")
    assert b.select({"A": history(-0.005)}) == ("rsi_reversion", "down")
    assert b.select({"A": history(0)}) == ("rsi_reversion", "range")
    assert b.select({"A": history()}, retired={"sma_trend"}) == (None, "up")
    assert b.select({"A": history()[:10]}) == (None, "warming_up")
    assert b.select({"A": history(start=datetime(2027, 1, 1, tzinfo=UTC))}) == (None, "up")


def test_no_future_benchmark_bars_or_future_eligibility_are_used():
    m = manifest()
    rows = history()
    rows_with_future = [*rows, (rows[-1][0] + 86400 * 10**9, 1)]
    assert bank(m).select({"A": rows_with_future, "B": rows}) == ("sma_trend", "up")
    m["entries"][0]["eligible_after"] = "2025-03-01T00:00:00+00:00"
    assert bank(m).select({"A": rows}) == (None, "up")
    m["entries"][0]["eligible_after"] = "2024-01-01T00:00:00+00:00"
    m["entries"][0]["retired_at"] = "2025-01-10T00:00:00+00:00"
    assert bank(m).select({"A": rows}) == (None, "up")


def test_tampered_artifacts_and_ambiguous_eligibility_are_rejected():
    m = manifest()
    invalid = []
    a = deepcopy(m)
    a["entries"][0]["artifact"] = "other"
    invalid.append(a)
    a = deepcopy(m)
    a["entries"][0]["regimes"] = ["unknown"]
    invalid.append(a)
    a = deepcopy(m)
    a["entries"][0]["expires_at"] = "2023-01-01T00:00:00+00:00"
    invalid.append(a)
    a = deepcopy(m)
    a["window"] = True
    invalid.append(a)
    for a in invalid:
        with pytest.raises(ValueError):
            bank(a)


def test_bank_runs_in_same_continuous_backtest_allocator(goog_lab, goog):
    from lab import backtest

    specs = [SignalSpec("buy_and_hold"), SignalSpec("sma_trend", {"fast": 3, "slow": 10})]
    m = manifest()
    m["benchmark"] = "GOOG"
    m["entries"] = [
        {
            "name": s.name,
            "artifact": digest({"name": s.name, "params": s.params, "sha256": s.sha256}),
            "priority": i,
            "regimes": ["up", "down", "range", "volatile"],
            "eligible_after": "2000-01-01T00:00:00+00:00",
            "expires_at": "2030-01-01T00:00:00+00:00",
        }
        for i, s in enumerate(specs)
    ]
    policy = StrategyBank(m, specs, ["GOOG"])
    start, end = goog.index[556].date(), goog.index[565].date()
    actual = backtest.run(
        goog_lab, [backtest.Sleeve(s.make(), "GOOG", 0.5) for s in specs], {"GOOG": goog}, start, end, bank=policy
    )
    expected = backtest.run(goog_lab, [backtest.Sleeve(specs[0].make(), "GOOG", 0.5)], {"GOOG": goog}, start, end)
    assert actual.fills and [(f.side, f.qty, f.price) for f in actual.fills] == [
        (f.side, f.qty, f.price) for f in expected.fills
    ]
    assert actual.equity.equals(expected.equity)


def test_bank_retirement_survives_allocator_restart(tmp_path):
    from types import MethodType, SimpleNamespace

    from lab.agents.allocator import AllocatorAgent, AllocatorAgentConfig
    from lab.agents.messages import RiskEvent

    state = tmp_path / "allocator.json"
    double = SimpleNamespace(
        config=SimpleNamespace(state_path=str(state), vol_window=20),
        retired=set(),
        groups={"sma_trend": ["sma_trend:A"]},
        closes={"A": []},
        selection=(None, "warming_up"),
        weights=lambda: {"A": 0},
        publish_message=lambda *_: None,
    )
    double._save = MethodType(AllocatorAgent._save, double)
    AllocatorAgent._on_risk(double, RiskEvent(1, "signal_failed", "sma_trend"))
    restarted = AllocatorAgent(
        AllocatorAgentConfig(sleeves={"sma_trend:A": ("A", 1)}, vol_window=20, state_path=str(state))
    )
    assert restarted.retired == {"sma_trend"}
