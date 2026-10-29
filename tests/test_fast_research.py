"""Research on recorded data: a forward holdout research can't see, one holdout test per configuration, and
every run counted."""

import json
import math
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event

import numpy as np
import pytest
from nautilus_trader.model import CryptoPerpetual, Currency, InstrumentId, OrderBookDelta, Price, Quantity, Symbol
from nautilus_trader.persistence import ParquetDataCatalog

from lab import fast_research as fr
from lab.cli import main as lab_main
from lab.fast import backtest
from tests.conftest import REPO
from tests.test_fast import IID, USDT, cfg, tilted_market

SPLIT = 2000  # steps recorded before the first research run; the rest arrive afterwards


def catalog_with(path, deltas):
    path.mkdir()
    catalog = ParquetDataCatalog(str(path))
    inst = CryptoPerpetual(
        InstrumentId.from_str(IID), Symbol("BTCUSDT"), Currency.from_str("BTC"), USDT, USDT, False, 1, 3,
        Price.from_str("0.1"), Quantity.from_str("0.001"), 0, 0,
    )  # fmt: skip
    catalog.write_instruments([inst])
    catalog.write_order_book_deltas(deltas)
    return str(path)


@pytest.fixture
def recording(tmp_path):
    """A catalog holding the first SPLIT steps; `more()` records the rest, as the recorder would later."""
    deltas, *_ = tilted_market(3000)
    cut = next(i for i, d in enumerate(deltas) if d.ts_init >= SPLIT * 100 * 1_000_000)
    path = catalog_with(tmp_path / "catalog", deltas[:cut])

    def more():
        ParquetDataCatalog(path).write_order_book_deltas(deltas[cut:])

    return path, tmp_path / "results", deltas[cut - 1].ts_init, more


def test_first_research_run_reserves_everything_recorded_later(recording):
    path, results, last_ts, more = recording
    b = fr.boundary(results, path, IID)
    assert b == last_ts + 1
    more()
    assert fr.boundary(results, path, IID) == b  # new data lands in the holdout, not in research
    start, end = fr.research_window(results, path, IID, None, None)
    assert end == b - 1
    r = backtest(path, cfg(), start=start, end=end, taker_bps=0, maker_bps=0, latency_ms=0)
    assert r["end_ns"] < b
    for window in ((b, None), (None, b), (b + 10, b + 20)):
        with pytest.raises(fr.HoldoutGuard):
            fr.research_window(results, path, IID, *window)


def test_a_configuration_meets_the_holdout_once(recording):
    path, results, _, more = recording
    more()
    b = fr.boundary(results, path, IID)
    params = {"threshold": 0.6}
    with fr.holdout_run(results, IID, params, b, holdout_end=b + 10) as attempt:
        attempt.result = {"pnl": 1.0}
    with pytest.raises(fr.HoldoutGuard, match="--again"):
        fr.check_holdout(results, IID, params, b)
    fr.check_holdout(results, IID, params, b, again="fixed a fee bug")  # allowed, and recorded by the caller
    with pytest.raises(fr.HoldoutGuard, match="--again"):
        fr.check_holdout(results, IID, {"threshold": 0.7}, b)  # changing the configuration is holdout reuse too


def _holdout_partition(results):
    fr.atomic_json(results / fr.PARTITION, {"version": 1, "instruments": {IID: {"holdout_start": 100}}})


def _compete_for_holdout(results, barrier, outcomes):
    barrier.wait(timeout=15)
    try:
        with fr.holdout_run(results, IID, {"threshold": 0.6}, 100, holdout_end=200) as attempt:
            attempt.result = {"pnl": 1.0}
        outcomes.put("completed")
    except fr.HoldoutGuard:
        outcomes.put("denied")


def _die_during_holdout(results):
    with fr.holdout_run(results, IID, {"threshold": 0.6}, 100, holdout_end=200):
        os._exit(23)  # no context-manager cleanup; the kernel releases the lock


def test_two_processes_cannot_claim_the_same_holdout(tmp_path):
    assert hasattr(fr, "holdout_run")
    _holdout_partition(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    barrier, outcomes = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_compete_for_holdout, args=(tmp_path, barrier, outcomes)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert sorted(outcomes.get(timeout=5) for _ in processes) == ["completed", "denied"]
        state = fr.read_json(tmp_path / fr.HOLDOUT_STATE)
        assert [a["status"] for a in state["attempts"]] == ["completed"]
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        outcomes.close()


def test_a_process_exit_leaves_the_holdout_used_even_for_different_parameters(tmp_path):
    assert hasattr(fr, "holdout_run")
    _holdout_partition(tmp_path)
    process = multiprocessing.get_context("spawn").Process(target=_die_during_holdout, args=(tmp_path,))
    process.start()
    try:
        process.join(timeout=20)
        assert process.exitcode == 23
        state = fr.read_json(tmp_path / fr.HOLDOUT_STATE)
        assert state["attempts"][0]["status"] == "reserved"
        with (
            pytest.raises(fr.HoldoutGuard, match="--again"),
            fr.holdout_run(tmp_path, IID, {"threshold": 0.7}, 100, holdout_end=200),
        ):
            pytest.fail("the interrupted holdout was silently reused")
        with fr.holdout_run(
            tmp_path, IID, {"threshold": 0.7}, 100, holdout_end=200, again="retry after crash"
        ) as attempt:
            assert attempt.reused
            attempt.result = {"pnl": -1.0}
        state = fr.read_json(tmp_path / fr.HOLDOUT_STATE)
        assert [a["status"] for a in state["attempts"]] == ["reserved", "completed"]
        assert state["attempts"][1]["again"] == "retry after crash"
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def test_holdout_failure_is_persisted_and_cannot_be_silently_retried(tmp_path):
    _holdout_partition(tmp_path)
    with (
        pytest.raises(RuntimeError, match="interrupted"),
        fr.holdout_run(tmp_path, IID, {"threshold": 0.6}, 100, holdout_end=200),
    ):
        state = fr.read_json(tmp_path / fr.HOLDOUT_STATE)
        assert state["attempts"][0]["status"] == "reserved"
        raise RuntimeError("interrupted")
    state = fr.read_json(tmp_path / fr.HOLDOUT_STATE)
    assert state["attempts"][0]["status"] == "failed"
    with (
        pytest.raises(fr.HoldoutGuard, match="--again"),
        fr.holdout_run(tmp_path, IID, {"threshold": 0.6}, 100, holdout_end=200),
    ):
        pytest.fail("a failed attempt must remain used")


def test_legacy_holdout_results_also_freeze_new_configurations(tmp_path):
    _holdout_partition(tmp_path)
    fr.append_json(
        tmp_path / fr.LEDGER,
        {
            "at": "2026-10-29",
            "instrument": IID,
            "fingerprint": fr.fingerprint({"threshold": 0.6}),
            "params": {"threshold": 0.6},
            "holdout_start": 100,
            "again": "",
            "result": {"pnl": 1},
        },
    )
    with pytest.raises(fr.HoldoutGuard, match="--again"):
        fr.check_holdout(tmp_path, IID, {"threshold": 0.7}, 100)


def test_holdout_reservation_rejects_a_boundary_changed_since_configuration(tmp_path):
    _holdout_partition(tmp_path)
    with (
        pytest.raises(fr.HoldoutGuard, match="boundary changed"),
        fr.holdout_run(tmp_path, IID, {}, 99, holdout_end=200),
    ):
        pytest.fail("stale boundary allowed")


def test_corrupt_holdout_state_blocks_access(tmp_path):
    _holdout_partition(tmp_path)
    (tmp_path / fr.HOLDOUT_STATE).write_text("{")
    with pytest.raises(fr.HoldoutGuard, match="unreadable"), fr.holdout_run(tmp_path, IID, {}, 100, holdout_end=200):
        pytest.fail("corrupt state allowed")


@pytest.mark.parametrize(
    "raw", ['{"instrument":"X","instrument":"Y"}', '{"holdout_start":NaN}', '{"holdout_start":"100"}']
)
def test_corrupt_legacy_holdout_ledger_cannot_be_ignored(tmp_path, raw):
    _holdout_partition(tmp_path)
    (tmp_path / fr.LEDGER).write_text(raw + "\n")
    with pytest.raises(fr.HoldoutGuard, match="unreadable"), fr.holdout_run(tmp_path, IID, {}, 100, holdout_end=200):
        pytest.fail("invalid legacy data allowed")


def test_reservation_write_failure_prevents_holdout_access(tmp_path, monkeypatch):
    _holdout_partition(tmp_path)

    def no_space(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(fr, "atomic_json", no_space)
    with pytest.raises(OSError, match="disk full"), fr.holdout_run(tmp_path, IID, {}, 100, holdout_end=200):
        pytest.fail("data access began without a durable reservation")


def test_configuration_is_frozen_before_the_evaluator_can_mutate_its_input(tmp_path):
    _holdout_partition(tmp_path)
    params = {"thresholds": [0.6]}
    with fr.holdout_run(tmp_path, IID, params, 100, holdout_end=200) as attempt:
        assert not attempt.reused
        params["thresholds"][0] = 0.9
        attempt.result = {"pnl": -1}
    entry = fr.read_json(tmp_path / fr.HOLDOUT_STATE)["attempts"][0]
    assert entry["params"] == {"thresholds": [0.6]}
    assert entry["fingerprint"] == fr.fingerprint({"thresholds": [0.6]})
    assert not entry["reused"]


def test_completion_write_failure_leaves_the_durable_reservation(tmp_path, monkeypatch):
    _holdout_partition(tmp_path)
    with pytest.raises(OSError, match="disk full"), fr.holdout_run(tmp_path, IID, {}, 100, holdout_end=200) as attempt:
        attempt.result = {"pnl": 1}

        def no_space(*_args):
            raise OSError("disk full")

        monkeypatch.setattr(fr, "atomic_json", no_space)
    assert fr.read_json(tmp_path / fr.HOLDOUT_STATE)["attempts"][0]["status"] == "reserved"
    with pytest.raises(fr.HoldoutGuard, match="--again"):
        fr.check_holdout(tmp_path, IID, {}, 100)


def test_holdout_release_waits_until_the_evaluation_finishes(recording):
    path, results, _, more = recording
    start = fr.boundary(results, path, IID)
    more()
    end = fr.last_recorded(path, IID)
    started = Event()

    def release():
        started.set()
        return fr.release(results, path, IID, "evaluation finished")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with fr.holdout_run(results, IID, {}, start, holdout_end=end) as attempt:
            future = pool.submit(release)
            assert started.wait(timeout=5)
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.1)
            attempt.result = {"pnl": 1}
        assert future.result(timeout=10) == (start, end + 1)


def test_fast_cli_reserves_before_evaluation_and_records_failure(recording, tmp_path, monkeypatch):
    from lab import fast

    path, results, _, more = recording
    fr.boundary(results, path, IID)
    more()
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    calls = []

    def fail(*_args, **kw):
        calls.append(kw)
        state = fr.read_json(results / fr.HOLDOUT_STATE)
        assert state["attempts"][-1]["status"] == "reserved"
        assert state["attempts"][-1]["holdout_end"] == kw["end"]
        raise RuntimeError("injected evaluator failure")

    monkeypatch.setattr(fast, "backtest", fail)
    run = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path, "--holdout"]
    with pytest.raises(RuntimeError, match="injected"):
        lab_main(run)
    with pytest.raises(SystemExit, match="--again"):
        lab_main([*run, "--threshold", "0.7"])
    assert len(calls) == 1
    assert fr.read_json(results / fr.HOLDOUT_STATE)["attempts"][0]["status"] == "failed"


def test_fast_holdout_end_does_not_move_when_the_recorder_appends_data(recording, tmp_path, monkeypatch):
    from lab import fast

    path, results, _, more = recording
    fr.boundary(results, path, IID)
    more()
    frozen_end = fr.last_recorded(path, IID)
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    evaluate = fast.backtest

    def append_then_evaluate(*args, **kw):
        extra, *_ = tilted_market(10)
        extra = [
            OrderBookDelta(
                d.instrument_id,
                d.action,
                d.order,
                d.flags,
                d.sequence,
                d.ts_event + 400_000_000_000,
                d.ts_init + 400_000_000_000,
            )
            for d in extra
        ]
        ParquetDataCatalog(path).write_order_book_deltas(extra)
        assert fr.last_recorded(path, IID) > frozen_end
        assert kw["end"] == frozen_end
        result = evaluate(*args, **kw)
        assert result["end_ns"] <= frozen_end
        return result

    monkeypatch.setattr(fast, "backtest", append_then_evaluate)
    run = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path, "--holdout"]
    assert lab_main(run) == 0
    attempt = fr.read_json(results / fr.HOLDOUT_STATE)["attempts"][0]
    assert attempt["holdout_end"] == frozen_end and attempt["status"] == "completed"


def test_release_moves_the_boundary_forward_with_a_reason(recording):
    path, results, _, more = recording
    old = fr.boundary(results, path, IID)
    with pytest.raises(fr.HoldoutGuard, match="nothing has been recorded"):
        fr.release(results, path, IID, "more data")
    more()
    with pytest.raises(ValueError, match="reason"):
        fr.release(results, path, IID, " ")
    was, new = fr.release(results, path, IID, "a week more data")
    assert was == old < new == fr.boundary(results, path, IID)
    history = json.loads((results / fr.PARTITION).read_text())["instruments"][IID]["history"]
    assert [h["reason"] for h in history] == ["first research run", "a week more data"]


def test_trials_count_distinct_configurations_on_research_data(tmp_path):
    def run(params, holdout=False):
        return fr.register_trial(tmp_path, "fast-backtest", IID, params, (0, 1), {}, holdout=holdout)

    assert run({"threshold": 0.6}) == 1
    assert run({"threshold": 0.6}) == 1  # a rerun is not a new trial
    assert run({"threshold": 0.7}) == 2
    assert run({"threshold": 0.8}, holdout=True) == 2
    assert fr.register_trial(tmp_path, "micro-study", IID, {"every_ms": 100}, (0, 1), {}) == 1


def test_daily_equity_statistics_match_hand_calculated_net_returns():
    returns = np.array([0.01, -0.01, 0.02, -0.005])
    equity = 100 * np.cumprod(1 + returns)
    stats = fr.equity_return_stats(dict(enumerate(equity)), 100.0)
    assert list(stats["daily_returns"].values()) == pytest.approx(returns)
    assert stats["days"] == 4
    assert stats["sr_daily"] == pytest.approx(returns.mean() / returns.std(ddof=1))
    assert stats["sharpe"] == pytest.approx(stats["sr_daily"] * math.sqrt(365))
    assert fr.equity_return_stats({0: 100.0, 1: 100.0}, 100.0)["sr_daily"] is None


@pytest.mark.parametrize("bad", [None, math.nan, math.inf])
def test_missing_equity_does_not_become_a_zero_return_or_a_sharpe_estimate(bad):
    stats = fr.equity_return_stats({0: 101.0, 1: bad, 2: 102.0, 3: 104.0}, 100.0)
    assert list(stats["daily_returns"].values())[1:3] == [None, None]
    assert stats["sr_daily"] is stats["sharpe"] is None
    json.dumps(stats, allow_nan=False)


def test_a_missing_calendar_day_is_not_annualized_as_one_daily_return():
    stats = fr.equity_return_stats({0: 101.0, 2: 104.0, 3: 105.0, 4: 106.0}, 100.0)
    assert stats["daily_returns"]["1970-01-03"] is None
    assert "1970-01-02" not in stats["daily_returns"]
    assert stats["sr_daily"] is None


def test_shared_deflated_sharpe_math_matches_the_original_paper_example():
    from lab.carry_study import expected_max_sharpe, probabilistic_sharpe

    # Bailey & Lopez de Prado (2014), "A Numerical Example": N=100, variance=0.002, T=1250.
    trials = np.arange(100, dtype=float)
    trials = (trials - trials.mean()) / trials.std(ddof=1) * math.sqrt(0.002)
    benchmark = expected_max_sharpe(trials.tolist())
    assert benchmark == pytest.approx(0.1132, abs=5e-5)
    assert probabilistic_sharpe(2.5 / math.sqrt(250), 1250, -3.0, 10.0, benchmark) == pytest.approx(0.9004, abs=5e-5)


def test_deflated_fast_sharpe_needs_complete_comparable_research_trials(tmp_path):
    from lab.carry_study import expected_max_sharpe, probabilistic_sharpe

    def trial(params, equities, *, holdout=False, window=(0, 10)):
        stats = fr.equity_return_stats(dict(enumerate(equities)), 100.0)
        fr.register_trial(tmp_path, "fast-backtest", IID, params, window, {"return_stats": stats}, holdout=holdout)
        return stats

    a = trial({"threshold": 0.6}, [101, 100, 102, 101, 103])
    score, reason = fr.fast_deflated_sharpe(tmp_path, IID, (0, 10), a)
    assert score is None and "two" in reason
    b = trial({"threshold": 0.7}, [100, 99, 101, 100, 102])
    score, reason = fr.fast_deflated_sharpe(tmp_path, IID, (0, 10), b)
    benchmark = expected_max_sharpe([a["sr_daily"], b["sr_daily"]])
    assert score == pytest.approx(probabilistic_sharpe(b["sr_daily"], 5, b["skew"], b["kurt"], benchmark))
    assert reason == ""
    trial({"threshold": 0.8}, [100, 100, 100, 100, 100], holdout=True)
    assert fr.fast_deflated_sharpe(tmp_path, IID, (0, 10), b)[0] == score  # holdout never tunes research
    fr.register_trial(tmp_path, "fast-backtest", IID, {"threshold": 0.9}, (0, 10), {"pnl": 1})
    score, reason = fr.fast_deflated_sharpe(tmp_path, IID, (0, 10), b)
    assert score is None and "missing" in reason  # don't silently drop old trials that lack daily returns
    trial({"threshold": 0.9}, [100, 99, 98, 99, 101], window=(0, 11))
    score, reason = fr.fast_deflated_sharpe(tmp_path, IID, (0, 10), b)
    assert score is None and "window" in reason


def test_cli_prints_a_diagnostic_only_after_comparable_daily_research_trials(tmp_path, capsys, monkeypatch):
    from lab import fast

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    monkeypatch.setattr(fr, "research_window", lambda *_args: (0, 5 * fr.DAY_NS))

    def backtest(_catalog, config, **_kwargs):
        import pandas as pd

        _kwargs["evidence"].series(
            "synthetic-market", pd.Series([1.0, 2.0], index=pd.date_range("2026-01-01", periods=2, tz="UTC"))
        )
        equity = [101, 100, 102, 101, 103] if config.threshold == 0.6 else [100, 99, 101, 100, 102]
        return {
            "entries": 5,
            "pnl": equity[-1] - 100,
            "cash_pnl": equity[-1] - 100,
            "unrealized_pnl": 0,
            "terminal": {"net_qty": 0, "outstanding_orders": 0, "valuation_ns": 5 * fr.DAY_NS},
            "recording_segments": 1,
            "commissions": 0.5,
            "round_trips": 5,
            "mean_trip": 0.2,
            "t_stat": 1.0,
            "stopped": None,
            "fills": 10,
            "start_ns": 0,
            "end_ns": 5 * fr.DAY_NS,
            "return_stats": fr.equity_return_stats(dict(enumerate(equity)), 100.0),
        }

    monkeypatch.setattr(fast, "backtest", backtest)
    cmd = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", str(catalog)]
    assert lab_main(cmd) == 0
    assert "deflated Sharpe unavailable" in capsys.readouterr().out
    assert lab_main([*cmd, "--threshold", "0.7"]) == 0
    out = capsys.readouterr().out
    assert "deflated Sharpe unavailable" not in out
    assert "IID diagnostic, 2 research configurations" in out
    assert "no gate pass implied" in out
    trials = [json.loads(line) for line in (tmp_path / "results" / fr.TRIALS).read_text().splitlines()]
    assert all(len(t["result"]["return_stats"]["daily_returns"]) == 5 for t in trials)


def test_backtest_reports_round_trips_net_of_costs(recording):
    path, *_ = recording
    r = backtest(path, cfg(), taker_bps=5, maker_bps=2, latency_ms=0)
    assert r["round_trips"] == r["entries"] - 1  # the last entry is still open at the recording's end
    assert abs(r["terminal"]["net_qty"]) == pytest.approx(0.01)
    # Closed trips exclude the terminal position's entry commission and unrealized mark.
    assert r["mean_trip"] * r["round_trips"] == pytest.approx(r["cash_pnl"] + 0.005, abs=0.001)
    assert r["pnl"] == pytest.approx(r["cash_pnl"] + r["unrealized_pnl"])
    assert r["t_stat"] < 0  # 5 bp a side is more than the planted edge


def test_cli_research_then_one_holdout_test(recording, tmp_path, capsys):
    path, _, _, more = recording
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    run = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path]

    assert lab_main([*run, "--size", "0.01", "--hold", "1"]) == 0
    out = capsys.readouterr().out
    assert "research data; configurations tried on research data: 1" in out
    assert "round trips" in out
    assert "deflated Sharpe unavailable" in out
    more()
    assert lab_main([*run, "--holdout"]) == 0
    assert "HOLDOUT test" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="--again"):
        lab_main([*run, "--holdout"])
    assert lab_main([*run, "--holdout", "--again", "checking the report"]) == 0
    assert "HOLDOUT REUSE (reason recorded): checking the report" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="holdout"):
        lab_main([*run, "--start", "2000-01-01", "--end", "2100-01-01"])
    ledger = (tmp_path / "results" / fr.LEDGER).read_text().splitlines()
    assert [json.loads(e)["again"] for e in ledger] == ["", "checking the report"]


def test_micro_study_reads_research_data_only(recording, tmp_path, capsys):
    path, _, _, more = recording
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    study = ["--config", str(tmp_path / "lab.toml"), "micro-study", "BTCUSDT", "--catalog", path]

    assert lab_main(study) == 0
    first = capsys.readouterr().out
    more()
    assert lab_main(study) == 0
    assert capsys.readouterr().out == first  # the new data is holdout
    assert "study settings tried: 1" in first
    with pytest.raises(SystemExit, match="holdout"):
        lab_main([*study, "--end", "2100-01-01"])


def test_micro_study_keeps_the_leaders_holdout_unseen_too(recording, tmp_path, capsys):
    path, results, last_ts, _ = recording
    leader = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    deltas, *_ = tilted_market(1500)  # the leader's recording stops earlier, so its holdout starts earlier
    ParquetDataCatalog(path).write_order_book_deltas(
        [OrderBookDelta(leader, d.action, d.order, d.flags, d.sequence, d.ts_event, d.ts_init) for d in deltas]
    )
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    study = ["--config", str(tmp_path / "lab.toml"), "micro-study", "BTCUSDT", "--catalog", path]
    assert lab_main([*study, "--leader", "BTCUSDT", "--leader-venue", "bybit"]) == 0
    assert "lead_lag_bps" in capsys.readouterr().out
    assert fr.boundary(results, path, str(leader)) == deltas[-1].ts_init + 1
    trial = json.loads((results / fr.TRIALS).read_text().splitlines()[-1])
    assert trial["window"][1] == deltas[-1].ts_init < last_ts  # cut at the leader's holdout, not the symbol's
    assert trial["params"]["leader"] == str(leader)
    with pytest.raises(SystemExit, match="another symbol or venue"):
        lab_main([*study, "--leader", "BTCUSDT"])
