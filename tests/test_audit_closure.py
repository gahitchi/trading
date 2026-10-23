"""Adversarial regressions for G009's second audit pass; no broker or network."""

import itertools
import json
import math
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from lab.agents.allocator import AllocatorAgent, AllocatorAgentConfig
from lab.agents.messages import PriceMark
from lab.config import ConfigError, MarginSettings, RiskSettings, validate_effective
from lab.persistence import atomic_json, read_json
from lab.signals.registry import SignalSpec


def allocator(tmp_path=None, window=30):
    return AllocatorAgent(
        AllocatorAgentConfig(
            sleeves={"s:A": ("A", 0.5), "s:B": ("B", 0.5)},
            target_vol=0.15,
            vol_window=window,
            max_leverage=2,
            state_path=str(tmp_path / "vol.json") if tmp_path else None,
        )
    )


def test_volatility_aligns_both_return_endpoints():
    a = allocator(window=40)
    for i in range(40):
        a._on_mark(PriceMark("A", 100 * math.exp(0.02 * (i % 2)), i))
        if i != 12:
            a._on_mark(PriceMark("B", 100 * math.exp(0.03 * (i % 3)), i))
    weights = {"A": 0.5, "B": 0.5}
    rows = {t: {(x[0], y[0]): math.log(y[1] / x[1]) for x, y in itertools.pairwise(a.closes[t])} for t in weights}
    common = sorted(rows["A"].keys() & rows["B"].keys())
    portfolio_returns = np.array([(rows["A"][k] + rows["B"][k]) / 2 for k in common])
    expected = 0.15 / (portfolio_returns.std(ddof=1) * math.sqrt(252))
    assert a._vol_scale(weights) == pytest.approx(expected)
    assert (11, 13) not in common  # a two-day move must not be paired with a one-day move


def test_volatility_warmup_survives_restart_and_requires_every_asset(tmp_path):
    a = allocator(tmp_path)
    for i in range(25):
        a._on_mark(PriceMark("A", 100 + i % 3, i))
    assert a._vol_scale({"A": 0.5, "B": 0.5}) == 0
    for i in range(25):
        a._on_mark(PriceMark("B", 100 + i % 2, i))
    before = a._vol_scale({"A": 0.5, "B": 0.5})
    assert before > 0
    b = allocator(tmp_path)
    assert b._vol_scale({"A": 0.5, "B": 0.5}) == before
    with pytest.raises(ValueError, match="positive mark"):
        b._on_mark(PriceMark("B", float("nan"), 26))
    assert b.close_ts["B"] == 24


def test_missing_volume_rejected_zero_volume_never_creates_trade():
    from nautilus_trader.model import TradeTick

    from lab import data, universe

    frame = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [np.nan]},
        index=pd.to_datetime(["2024-01-02"]),
    )
    with pytest.raises(data.DataError, match="volume"):
        data.validate(frame, "X")
    frame["volume"] = 0
    clean = data.validate(frame, "X")
    iid = universe.instrument_id("X", "XNAS")
    events = universe.to_market_events(clean, iid, 0.1)
    assert not any(isinstance(e, TradeTick) for e in events)
    assert all(float(e.volume) == 0 for e in events)


@pytest.mark.parametrize("values", [[100, 0, 5], [100, -5, 10], [0, 5, 10]])
def test_insolvent_curve_cannot_receive_risk_ratios(values):
    from lab.metrics import summarize

    m = summarize(pd.Series(values, index=pd.date_range("2024-01-01", periods=3)))
    assert m["insolvent"] and not m["qualified"]
    assert all(m[k] is None for k in ("sharpe", "sortino", "calmar", "volatility", "cagr"))


def test_monthly_report_includes_first_month_and_compounds_year():
    from lab.report import _monthly_table

    table = _monthly_table(pd.Series([100, 120, 132], index=pd.to_datetime(["2024-01-05", "2024-01-31", "2024-02-29"])))
    assert ">+20.0</td>" in table and ">+10.0</td>" in table
    assert "class='yr'>+32.0</td>" in table


def test_failed_holdout_is_exposure_and_parameter_change_cannot_retry(goog_lab):
    from lab.experiments import HoldoutUsed
    from lab.research_state import holdout_attempt, holdouts

    period = goog_lab.period("holdout")
    with (
        pytest.raises(RuntimeError, match="strategy failed"),
        holdout_attempt(goog_lab, [SignalSpec("sma_trend", {})], period, "daily", ""),
    ):
        raise RuntimeError("strategy failed after reading data")
    assert holdouts(goog_lab)[0]["status"] == "failed"
    with (
        pytest.raises(HoldoutUsed, match="already been exposed"),
        holdout_attempt(goog_lab, [SignalSpec("sma_trend", {"fast": 7})], period, "daily", ""),
    ):
        pytest.fail("exposed the same holdout again")
    with holdout_attempt(goog_lab, [SignalSpec("sma_trend", {})], period, "daily", "explicit diagnostic repeat"):
        pass
    assert [r["status"] for r in holdouts(goog_lab)] == ["failed", "completed"]


def test_intraday_boundary_cannot_be_backdated(goog_lab):
    from lab.experiments import HoldoutUsed, intraday_split

    frames = {"X": pd.DataFrame({"close": 1}, index=pd.bdate_range("2026-01-02", periods=20))}
    _, holdout = intraday_split(goog_lab, frames)
    cfg = replace(goog_lab, trading=replace(goog_lab.trading, intraday_holdout_start="2026-01-05"))
    with pytest.raises(HoldoutUsed, match="relabel exposed research"):
        intraday_split(cfg, frames)
    assert holdout.start > date(2026, 1, 5)


def test_reservations_reject_unsafe_one_sided_neutral_book():
    from lab.reservations import check_openings

    limits = RiskSettings(1, 2, 0.3, 1, -0.1, 0.1)
    # Final book is neutral; either order alone violates net limits.
    assert "net limit" in check_openings({}, {"A": 50, "B": -50}, {"A": 100, "B": 100}, 10000, limits, MarginSettings())


def test_reservations_cover_every_fractional_fill_corner():
    from lab.margin import initial_requirement
    from lab.reservations import check_openings

    limits = RiskSettings(0.6, 1, 0.3, 0.5, -0.3, 0.6)
    rng = np.random.default_rng(14)
    accepted = 0
    for _ in range(100):
        targets = dict(zip("ABC", rng.integers(-25, 26, size=3), strict=True))
        if check_openings({}, targets, dict.fromkeys("ABC", 100), 10000, limits, MarginSettings()) is not None:
            continue
        accepted += 1
        for fraction in itertools.product((0, 0.5, 1), repeat=3):
            w = {t: targets[t] * f / 100 for t, f in zip("ABC", fraction, strict=True)}
            assert max(map(abs, w.values())) <= limits.max_weight
            assert sum(map(abs, w.values())) <= limits.max_gross
            assert -sum(min(v, 0) for v in w.values()) <= limits.max_short_gross
            assert limits.min_net - 1e-9 <= sum(w.values()) <= limits.max_net + 1e-9
            assert initial_requirement(w, MarginSettings()) * 1.1 <= 1
    assert accepted > 10


def test_smoke_timeout_cannot_turn_cancel_ack_into_success(tmp_path):
    from lab.agents.smoke import SmokeTest

    class Fake:
        done = False
        intents = {"test-order": {}}
        checks = {"test order": {"ok": True, "detail": "cancel ack"}}
        config = SimpleNamespace(report_path=str(tmp_path / "report.json"))
        log = SimpleNamespace(info=lambda _: None)
        _check = SmokeTest._check

        def shutdown_system(self, _):
            return None

    smoke = Fake()
    SmokeTest._finish(smoke)
    report = read_json(smoke.config.report_path)
    assert not report["passed"] and not report["checks"]["settled"]["ok"]


def test_notifications_survive_sender_restart(tmp_path, monkeypatch):
    import threading

    from lab.notify import Notifier

    # No real worker/network; storage is tested across separate instances.
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    path = str(tmp_path / "outbox.sqlite3")
    first = Notifier(token="test", chat_id="test", spool_path=path)
    first.send("hard halt")
    assert not first.flush(timeout=0)
    second = Notifier(token="test", chat_id="test", spool_path=path)
    assert second.pending() == 1

    class StopWorker(Exception):
        pass

    received = []

    def deliver(text):
        received.append(text)
        return True

    monkeypatch.setattr(second, "_post", deliver)
    monkeypatch.setattr(second._wake, "wait", lambda _: (_ for _ in ()).throw(StopWorker()))
    with pytest.raises(StopWorker):
        second._run_durable()
    assert received == ["[trading-lab] hard halt"] and second.pending() == 0


def test_calendar_holiday_early_close_and_dst():
    from lab.agents.schedule import Session
    from lab.exchange import bounds

    assert bounds(date(2026, 11, 26)) is None
    early = bounds(date(2026, 11, 27))
    assert early[1].hour == 13
    day = datetime(2026, 11, 27, 10, tzinfo=ZoneInfo("America/New_York"))
    assert Session(interval_min=15).close_at(day) == early[1]
    assert bounds(date(2026, 3, 6))[0].utcoffset().total_seconds() == -18000
    assert bounds(date(2026, 3, 9))[0].utcoffset().total_seconds() == -14400


def test_shortfall_uses_order_benchmark_and_cash_adjustment_disqualifies_return():
    from lab.paper_report import summarize

    rows = [
        {"type": "EquitySnapshot", "ts": 1, "equity": 100},
        {"type": "SignalUpdate", "price": 500, "ticker": "X", "ts": 2},
        {
            "type": "FillReport",
            "ticker": "X",
            "side": "BUY",
            "qty": 1,
            "price": 101,
            "commission": 1,
            "decision_price": 100,
            "decision_ts": 0,
        },
        {"type": "FillReport", "ticker": "X", "side": "BUY", "qty": 1, "price": 99, "commission": 1},
        {"type": "RiskEvent", "kind": "external_cash"},
        {"type": "EquitySnapshot", "ts": 3, "equity": 200},
    ]
    result = summarize(rows)
    assert result["shortfall_bps"]["mean"] == 100
    assert result["shortfall_unmeasured"] == 1
    assert result["total_return"] is None and not result["return_qualified"]


def test_atomic_state_rejects_nonfinite_without_losing_prior_state(tmp_path):
    p = tmp_path / "state.json"
    atomic_json(p, {"cash": 100})
    with pytest.raises(ValueError):
        atomic_json(p, {"cash": float("nan")})
    assert read_json(p) == {"cash": 100}
    p.write_text('{"cash": NaN}')
    with pytest.raises(ValueError, match="nonfinite"):
        read_json(p)


def test_programmatic_config_cannot_bypass_limits(goog_lab):
    with pytest.raises(ConfigError):
        validate_effective(replace(goog_lab, risk=replace(goog_lab.risk, max_weight=2)))
    with pytest.raises(ConfigError):
        validate_effective(replace(goog_lab, portfolio=replace(goog_lab.portfolio, vol_window=1)))


def test_scheduled_policy_keeps_the_same_book_through_transition(goog_lab):
    from lab import backtest
    from lab.signals.walk_forward import ScheduledSignal
    from tests.conftest import staircase

    frames = {"X": staircase(30)}
    switch = frames["X"].index[15].date()
    schedule = [(frames["X"].index[0].date(), SignalSpec("buy_and_hold", {})), (switch, SignalSpec("buy_and_hold", {}))]
    cfg = replace(goog_lab, warmup_start=date(2024, 1, 1))
    policy = backtest.run(
        cfg,
        [backtest.Sleeve(ScheduledSignal(schedule), "X", 1)],
        frames,
        frames["X"].index[0].date(),
        None,
        risk=RiskSettings(1, 1, 0.99),
    )
    control = backtest.run(
        cfg,
        [backtest.Sleeve(SignalSpec("buy_and_hold", {}).make(), "X", 1)],
        frames,
        frames["X"].index[0].date(),
        None,
        risk=RiskSettings(1, 1, 0.99),
    )
    pd.testing.assert_series_equal(policy.equity, control.equity)
    assert [(f.side, f.qty, f.commission) for f in policy.fills] == [
        (f.side, f.qty, f.commission) for f in control.fills
    ]


def test_undefined_holdout_statistics_are_json_null(goog_lab):
    from lab.experiments import Comparison, record_holdout

    comp = Comparison(
        "holdout", "2024-01-01", "2024-02-01", ["X"], [{"active_sharpe_ci": [float("nan"), float("nan")]}]
    )
    record_holdout(goog_lab, SignalSpec("buy_and_hold", {}), comp)
    row = json.loads((goog_lab.results_dir / "holdout_ledger.json").read_text())[0]
    assert row["rows"][0]["active_sharpe_ci"] == [None, None]


def test_two_concurrent_holdout_attempts_expose_only_once(goog_lab):
    from concurrent.futures import ThreadPoolExecutor

    from lab.experiments import HoldoutUsed
    from lab.research_state import holdout_attempt, holdouts

    def attempt(_):
        try:
            with holdout_attempt(goog_lab, [SignalSpec("sma_trend", {})], goog_lab.period("holdout"), "daily", ""):
                return "evaluated"
        except HoldoutUsed:
            return "refused"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results) == ["evaluated", "refused"]
    assert len(holdouts(goog_lab)) == 1


@pytest.mark.parametrize(
    "old,new",
    [
        ("slippage = true", 'slippage = "false"'),
        ("vol_window = 63", "vol_window = 63.5"),
        ("null_samples = 6", "null_samples = false"),
        ("[portfolio]", "[portfollo]"),
    ],
)
def test_raw_configuration_does_not_coerce_wrong_types(goog_lab, old, new):
    from lab.config import load_config

    path = goog_lab.root / "lab.toml"
    source = path.read_text()
    assert old in source
    path.write_text(source.replace(old, new))
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "name,params,minutes,delay",
    [
        ("orb", {}, 60, 15),
        ("intraday_momentum", {}, 15, 15),
        ("intraday_momentum", {}, 60, 0),
        ("vwap_reversion", {"min_minutes": 300}, 60, 60),
    ],
)
def test_unexecutable_intraday_preset_is_refused(name, params, minutes, delay):
    from lab.intraday import check_technique

    with pytest.raises(ValueError):
        check_technique(name, params, minutes, delay, True)


def test_final_backtest_equity_contains_last_mark_and_last_fills(goog_lab):
    from lab import backtest
    from lab.signals import BuyAndHold
    from tests.conftest import staircase

    frame = staircase(8)
    cfg = replace(goog_lab, warmup_start=date(2024, 1, 1))
    result = backtest.run(
        cfg,
        [backtest.Sleeve(BuyAndHold(), "X", 1)],
        {"X": frame},
        frame.index[0].date(),
        None,
        risk=RiskSettings(1, 1, 0.99),
    )
    shares = sum((1 if f.side == "BUY" else -1) * f.qty for f in result.fills)
    cash = cfg.capital - sum((1 if f.side == "BUY" else -1) * f.qty * f.price + f.commission for f in result.fills)
    assert result.equity.index[-1] == frame.index[-1]
    assert result.equity.iloc[-1] + result.financing == pytest.approx(cash + shares * frame.close.iloc[-1], abs=1e-6)


def test_zero_and_negative_experiment_counts_are_refused_before_work(goog_lab):
    from lab.experiments import null_test, walk_forward

    with pytest.raises(ValueError, match="positive"):
        null_test(goog_lab, SignalSpec("sma_trend", {}), ["GOOG"], goog_lab.period("train"), 0)
    with pytest.raises(ValueError, match="positive"):
        walk_forward(goog_lab, [], [], date(2000, 1, 1), date(2001, 1, 1), {}, is_years=1, oos_years=0)


def test_release_gate_does_not_equate_tests_with_release_readiness(tmp_path):
    from tools.release_check import blockers

    assert blockers({"version": 1, "gates": []}, tmp_path)
    assert blockers({"version": 1, "gates": [{"id": "broker", "status": "passed", "evidence": []}]}, tmp_path)
    path = __import__("pathlib").Path(__file__).resolve().parents[1]
    real = json.loads((path / "docs/release-criteria.json").read_text())
    assert len(blockers(real, path)) == 7
