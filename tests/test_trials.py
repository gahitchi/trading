"""An evaluation is an attempted configuration before it produces a favourable result."""

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from lab import fast_research as fr
from lab import trials
from tests.test_fast import IID


def _die(results):
    with trials.trial_run(results, "fast-backtest", IID, {"threshold": 0.7}, (0, 100)):
        os._exit(23)


def test_a_trial_is_durable_before_evaluation_and_its_configuration_is_frozen(tmp_path):
    params = {"thresholds": [0.6]}
    with trials.trial_run(tmp_path, "fast-backtest", IID, params, (0, 100)) as attempt:
        row = trials.history(tmp_path)[0]
        assert row["status"] == "reserved" and row["result"] == {}
        assert row["code"]["source_sha256"] and row["code"]["python"]
        params["thresholds"].append(0.7)
        attempt.result = {"pnl": -3}
        attempt.window = (1, 90)
    row = trials.history(tmp_path)[0]
    assert row["status"] == "completed" and row["result"]["pnl"] == -3
    assert row["params"] == {"thresholds": [0.6]}
    assert row["requested_window"] == [0, 100] and row["window"] == [1, 90]
    assert len((tmp_path / fr.TRIALS).read_text().splitlines()) == 1


def test_exception_and_missing_result_are_retained(tmp_path):
    with pytest.raises(ValueError, match="bad data"), trials.trial_run(tmp_path, "maker-study", IID, {}, (0, 100)):
        raise ValueError("bad data")
    with (
        pytest.raises(ValueError, match="without a result"),
        trials.trial_run(tmp_path, "maker-study", IID, {}, (0, 100)),
    ):
        pass
    rows = trials.history(tmp_path)
    assert len(rows) == 2 and all(r["status"] == "failed" for r in rows)
    assert all(r["result"]["status"] == "unavailable" for r in rows)
    assert trials.count_configurations(tmp_path, "maker-study", IID) == 1


def test_a_killed_evaluation_is_counted_and_blocks_the_sharpe_diagnostic(tmp_path):
    process = multiprocessing.get_context("spawn").Process(target=_die, args=(tmp_path,))
    process.start()
    try:
        process.join(timeout=20)
        assert process.exitcode == 23
        assert trials.history(tmp_path)[0]["status"] == "reserved"
        assert trials.count_configurations(tmp_path, "fast-backtest", IID) == 1
        score, reason = fr.fast_deflated_sharpe(tmp_path, IID, (0, 100), {})
        assert score is None and "incomplete" in reason
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def test_concurrent_attempts_survive_without_duplicate_completion_rows(tmp_path):
    def evaluate(i):
        with trials.trial_run(tmp_path, "fast-backtest", IID, {"threshold": i}, (0, 100)) as attempt:
            attempt.result = {"pnl": i}

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(evaluate, range(12)))
    assert len(trials.history(tmp_path)) == 12
    assert len({r["attempt_id"] for r in trials.history(tmp_path)}) == 12
    assert trials.count_configurations(tmp_path, "fast-backtest", IID) == 12


def test_reservation_failure_prevents_evaluation(tmp_path, monkeypatch):
    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(trials, "atomic_json", fail)
    with pytest.raises(OSError, match="disk full"), trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 1)):
        pytest.fail("evaluation started without a durable attempt")


def test_jsonl_failure_cannot_erase_a_completed_attempt(tmp_path, monkeypatch):
    def fail(*_args):
        raise OSError("append failed")

    monkeypatch.setattr(trials, "append_json", fail)
    with (
        pytest.raises(OSError, match="append failed"),
        trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 1)) as attempt,
    ):
        attempt.result = {"pnl": -10}
    row = trials.history(tmp_path)[0]
    assert row["status"] == "completed" and row["result"]["pnl"] == -10
    assert trials.count_configurations(tmp_path, "fast-backtest", IID) == 1


def test_failure_to_save_an_outcome_leaves_the_reserved_attempt_counted(tmp_path, monkeypatch):
    with (
        pytest.raises(OSError, match="disk full"),
        trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 1)) as attempt,
    ):

        def fail(*_args):
            raise OSError("disk full")

        monkeypatch.setattr(trials, "atomic_json", fail)
        attempt.result = {"pnl": 20}
    assert trials.history(tmp_path)[0]["status"] == "reserved"
    assert trials.count_configurations(tmp_path, "fast-backtest", IID) == 1


@pytest.mark.parametrize("raw", ['{"kind":"fast-backtest","kind":"carry-study"}', '{"x":NaN}', '{"x":1e999}', "{"])
def test_corrupt_or_truncated_trial_history_fails_closed(tmp_path, raw):
    (tmp_path / fr.TRIALS).write_text(raw)
    with pytest.raises(ValueError, match="trial history"):
        trials.history(tmp_path)


def test_corrupt_attempt_state_fails_closed(tmp_path):
    with trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 1)) as attempt:
        attempt.result = {}
    files = list((tmp_path / trials.ATTEMPTS).glob("*.json"))
    files[0].write_text('{"version":999}')
    with pytest.raises(ValueError, match="trial history"):
        trials.history(tmp_path)


def test_legacy_entries_remain_counted_and_holdouts_are_separate(tmp_path):
    fr.register_trial(tmp_path, "fast-backtest", IID, {"old": True}, (0, 1), {})
    with trials.trial_run(tmp_path, "fast-backtest", IID, {"new": True}, (0, 1)) as attempt:
        attempt.result = {}
    with trials.trial_run(tmp_path, "fast-backtest", IID, {"holdout": True}, (0, 1), holdout=True) as attempt:
        attempt.result = {}
    assert len(trials.history(tmp_path)) == 3
    assert trials.count_configurations(tmp_path, "fast-backtest", IID) == 2


def test_input_content_hash_captures_exact_values_and_order():
    def digest(values):
        evidence = trials.InputEvidence()
        evidence.series(
            "prices", pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values), freq="h", tz="UTC"))
        )
        return evidence.snapshot()

    a = digest([1.0, 2.0])
    assert a == digest([1.0, 2.0])
    assert a["sha256"] != digest([1.0, 2.0000000000000004])["sha256"]
    assert a["sha256"] != digest([2.0, 1.0])["sha256"]
    assert a["streams"][0]["records"] == 2


def test_source_hash_changes_without_relying_on_git(tmp_path):
    package = tmp_path / "lab"
    package.mkdir()
    source = package / "fast.py"
    source.write_text("x = 1\n")
    before = trials.code_provenance(package)
    source.write_text("x = 2\n")
    after = trials.code_provenance(package)
    assert before["source_sha256"] != after["source_sha256"]
    assert before["git_commit"] is None


@pytest.mark.parametrize("command", ["micro-study", "maker-study", "fast", "carry"])
def test_cli_reserves_before_each_evaluator_and_retains_general_failures(tmp_path, monkeypatch, command):
    from lab import carry_study, fast, maker_study, micro_study
    from lab.cli import main
    from tests.conftest import REPO
    from tests.test_carry import market
    from tests.test_maker import catalog, ranging_market

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    results = tmp_path / "results"
    args = ["--config", str(tmp_path / "lab.toml")]
    if command == "carry":
        market(tmp_path / "data" / "carry", days=400)
        module, name = carry_study, "run"
        args += ["carry", "study", "--symbols", "BTCUSDT"]
    else:
        path = catalog(tmp_path / "catalog", *ranging_market(steps=10))
        if command == "fast":
            module, name = fast, "backtest"
            args += ["fast", "backtest", "BTCUSDT", "--catalog", path]
        else:
            module, name = (micro_study if command == "micro-study" else maker_study), "study"
            args += [command, "BTCUSDT", "--catalog", path]

    def fail(*_args, **kw):
        rows = trials.history(results)
        assert len(rows) == 1 and rows[0]["status"] == "reserved"
        assert rows[0]["code"]["source_sha256"]
        assert isinstance(kw["evidence"], trials.InputEvidence)
        raise ValueError("injected evaluation failure")

    monkeypatch.setattr(module, name, fail)
    with pytest.raises(SystemExit, match="injected evaluation failure"):
        main(args)
    row = trials.history(results)[0]
    assert row["status"] == "failed" and "ValueError" in row["result"]["error"]


def test_a_successful_retry_cannot_hide_an_earlier_failed_attempt(tmp_path):
    with pytest.raises(ValueError), trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)):
        raise ValueError("failed attempt")
    with trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)) as attempt:
        attempt.result = {"return_stats": fr.equity_return_stats({i: 100 + i for i in range(5)}, 100)}
    score, reason = fr.fast_deflated_sharpe(tmp_path, IID, (0, 100), {})
    assert score is None and "incomplete" in reason
    assert len(trials.history(tmp_path)) == 2


@pytest.mark.parametrize("change", ["inputs", "code", "missing"])
def test_identical_windows_do_not_hide_changed_or_missing_provenance(tmp_path, monkeypatch, change):
    original = trials.code_provenance
    stats = fr.equity_return_stats({0: 101, 1: 100, 2: 102, 3: 101, 4: 103}, 100)

    def evaluate(params, value):
        with trials.trial_run(tmp_path, "fast-backtest", IID, params, (0, 100)) as attempt:
            attempt.inputs.series("prices", pd.Series([value], index=pd.date_range("2026-01-01", periods=1, tz="UTC")))
            attempt.result = {"return_stats": stats}

    evaluate({"threshold": 0.6}, 1)
    if change == "code":

        def changed():
            p = original()
            p["python"] += " changed-runtime"
            return p

        monkeypatch.setattr(trials, "code_provenance", changed)
    if change == "missing":
        fr.register_trial(tmp_path, "fast-backtest", IID, {"threshold": 0.7}, (0, 100), {"return_stats": stats})
    else:
        evaluate({"threshold": 0.7}, 2 if change == "inputs" else 1)
    score, reason = fr.fast_deflated_sharpe(tmp_path, IID, (0, 100), stats)
    assert score is None and ("provenance" in reason or "contents differ" in reason)


def test_carry_diagnostic_cannot_ignore_failed_or_incompatible_attempts(tmp_path):
    from lab.carry_study import trial_sharpes

    fr.register_trial(tmp_path, "carry-study", "carry-cash", {"entry": 10}, (0, 100), {"sr_daily": 0.1})
    fr.register_trial(tmp_path, "carry-study", "carry-cash", {"entry": 20}, (0, 100), {"sr_daily": 0.2})
    assert trial_sharpes(tmp_path, "carry-study", "carry-cash") == [0.1, 0.2]
    with pytest.raises(ValueError), trials.trial_run(tmp_path, "carry-study", "carry-cash", {"entry": 10}, (0, 100)):
        raise ValueError("unvalued position")
    with pytest.raises(ValueError, match="incomplete"):
        trial_sharpes(tmp_path, "carry-study", "carry-cash")


def test_native_market_content_hash_retains_price_precision_and_timestamps():
    from tests.test_maker import ranging_market

    deltas, trades = ranging_market(steps=2)
    evidence = trials.InputEvidence()
    evidence.objects("book", deltas)
    evidence.objects("trades", trades)
    snap = evidence.snapshot()
    assert snap["streams"][0]["first_ns"] == 0
    assert snap["streams"][0]["last_ns"] == 100_000_000
    assert snap["streams"][1]["records"] == 4
    again = trials.InputEvidence()
    again.objects("book", deltas)
    again.objects("trades", trades)
    assert snap == again.snapshot()
    changed = trials.InputEvidence()
    changed.objects("book", deltas)
    changed.objects("trades", trades[:-1])
    assert snap["sha256"] != changed.snapshot()["sha256"]


def test_appending_unseen_holdout_data_does_not_change_research_input_identity(tmp_path, capsys):
    from nautilus_trader.persistence import ParquetDataCatalog

    from lab.cli import main
    from tests.conftest import REPO
    from tests.test_fast_execution import shifted
    from tests.test_maker import MS, catalog, ranging_market

    path = catalog(tmp_path / "catalog", *ranging_market(steps=10))
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    args = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path]
    args += ["--strategy", "maker", "--size", "0.01", "--latency-ms", "0"]
    assert main(args) == 0
    extra_deltas, extra_trades = shifted(*ranging_market(steps=10, bid=500), 2000 * MS)
    c = ParquetDataCatalog(path)
    c.write_order_book_deltas(extra_deltas)
    c.write_trade_ticks(extra_trades)
    assert main(args) == 0
    rows = trials.history(tmp_path / "results")
    assert len(rows) == 2 and rows[0]["inputs"] == rows[1]["inputs"]
    assert all(s["last_ns"] is None or s["last_ns"] <= 900 * MS for s in rows[1]["inputs"]["streams"])
    capsys.readouterr()


def test_changing_cached_carry_holdout_values_does_not_change_research_input_identity(tmp_path, capsys):
    from lab import carry_data, carry_study
    from lab.cli import main
    from tests.conftest import REPO
    from tests.test_carry import market, write

    root = tmp_path / "data" / "carry"
    market(root, days=400)
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    args = ["--config", str(tmp_path / "lab.toml"), "carry", "study", "--symbols", "BTCUSDT"]
    assert main(args) == 0
    b = carry_study.boundary(tmp_path / "results", root)
    prices = carry_data.load(root, "binance", "perp", "close", "BTCUSDT")
    prices.loc[prices.index >= b] *= 1.5
    write(root, "binance", "perp", "close", "BTCUSDT", prices)
    assert main(args) == 0
    rows = trials.history(tmp_path / "results")
    assert len(rows) == 4
    by_config = {}
    for row in rows:
        assert all(s["last_ns"] is None or s["last_ns"] < b.value for s in row["inputs"]["streams"])
        old = by_config.setdefault(row["fingerprint"], row["inputs"])
        assert old == row["inputs"]
    capsys.readouterr()


def test_changed_code_or_runtime_is_a_distinct_configuration_even_with_identical_parameters(tmp_path, monkeypatch):
    with trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)) as attempt:
        attempt.result = {}
    original = trials.code_provenance

    def changed():
        p = original()
        p["python"] += " different-runtime"
        return p

    monkeypatch.setattr(trials, "code_provenance", changed)
    with trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)) as attempt:
        attempt.result = {}
    assert trials.count_configurations(tmp_path, "fast-backtest", IID) == 2


@pytest.mark.parametrize("target", ["code", "inputs", "mirror"])
def test_provenance_corruption_and_conflicting_mirrors_fail_closed(tmp_path, target):
    with trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)) as attempt:
        attempt.result = {"pnl": -1}
    path = next((tmp_path / trials.ATTEMPTS).glob("*.json"))
    row = json.loads(path.read_text())
    if target == "code":
        row["code"]["source_sha256"] = "0" * 64
    elif target == "inputs":
        row["inputs"]["sha256"] = "0" * 64
    else:
        row["result"]["pnl"] = 10
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="trial history"):
        trials.history(tmp_path)
