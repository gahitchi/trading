"""Synthetic-only durable poll-attempt evidence tests, no exchange calls."""

from __future__ import annotations

import json

import pytest

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_forward_attempts import (
    coverage_from_attempts,
    load_attempts,
    record_attempt_finish,
    record_attempt_start,
)
from lab.bearish_hybrid_forward_collector import PublicMinuteCapture, run_bounded_capture
from lab.bearish_hybrid_forward_price import record_forward_price

S = "ETHUSDT"
T = 1_700_006_400_000_000_000
SCHEDULE = T + MINUTE_NS
START = SCHEDULE + 5_000_000_000


def _clock(received_ns):
    return iter((received_ns - 10_000_000, received_ns)).__next__


def _price(root, received_ns=START + 1_000_000_000):
    ms0 = T // 1_000_000
    ms1 = SCHEDULE // 1_000_000
    raw = json.dumps(
        [
            [ms0, "200", "205", "195", "201", "10", ms0 + 59_999, "2000", 10, "5", "1000", "0"],
            [ms1, "201", "208", "198", "205", "11", ms1 + 59_999, "2100", 11, "6", "1200", "0"],
        ]
    ).encode()
    return record_forward_price(root, S, source=lambda _: raw, clock_ns=_clock(received_ns))


def test_successful_poll_intent_links_to_real_saved_receipt(tmp_path):
    price = _price(tmp_path)
    start = record_attempt_start(tmp_path, S, SCHEDULE, START, 0)
    assert start.oi_due and start.funding_due
    record_attempt_finish(
        tmp_path,
        start,
        finished_ns=START + 2_000_000_000,
        price_received_ns=price.received_ns,
        price_bar_end_ns=price.bar.end_ns,
    )
    entries = load_attempts(tmp_path, S)
    assert len(entries) == 1
    assert entries[0].start == start
    assert entries[0].finish.price_received_ns == price.received_ns
    report = coverage_from_attempts(entries)
    assert report.attempts == 1
    assert report.completed == 1
    assert report.price_received == 1
    assert report.price_missing == 0
    assert report.oi_due == 1
    assert report.oi_received == 0
    assert report.funding_due == 1
    assert report.qualified is False
    assert report.actual_notional == 0


def test_network_failure_is_durably_visible_without_fake_price(tmp_path):
    start = record_attempt_start(tmp_path, S, SCHEDULE, START, 0)
    record_attempt_finish(
        tmp_path,
        start,
        finished_ns=START + 2_000_000_000,
        network_failures=("price:URLError", "open-interest:TimeoutError"),
    )
    records = load_attempts(tmp_path, S)
    assert records[0].finish.price_received_ns is None
    assert records[0].finish.network_failures == ("price:URLError", "open-interest:TimeoutError")
    coverage = coverage_from_attempts(records)
    assert coverage.attempts == 1
    assert coverage.completed == 1
    assert coverage.price_missing == 1
    assert coverage.price_received == 0
    assert coverage.oi_received == 0


def test_crash_after_start_is_interrupted_not_silently_successful(tmp_path):
    record_attempt_start(tmp_path, S, SCHEDULE, START, 0)
    entries = load_attempts(tmp_path, S)
    assert len(entries) == 1
    assert entries[0].finish is None
    summary = coverage_from_attempts(entries)
    assert summary.interrupted == 1
    assert summary.completed == 0
    assert summary.price_missing == 1


def test_aborted_malformed_payload_is_not_relabelled_network_failure(tmp_path):
    start = record_attempt_start(tmp_path, S, SCHEDULE, START, 0)
    record_attempt_finish(tmp_path, start, finished_ns=START + 1_000_000_000, aborted_error="ValueError")
    entries = load_attempts(tmp_path, S)
    assert entries[0].finish.aborted_error == "ValueError"
    summary = coverage_from_attempts(entries)
    assert summary.aborted == 1 and summary.completed == 0


def test_forged_result_reference_does_not_pass_offline_audit(tmp_path):
    start = record_attempt_start(tmp_path, S, SCHEDULE, START, 0)
    record_attempt_finish(
        tmp_path,
        start,
        finished_ns=START + 2_000_000_000,
        price_received_ns=START + 1_000_000_000,
        price_bar_end_ns=SCHEDULE,
    )
    with pytest.raises(ValueError, match="missing/mismatched"):
        load_attempts(tmp_path, S)


def test_finished_record_cannot_be_overwritten(tmp_path):
    start = record_attempt_start(tmp_path, S, SCHEDULE, START, 0)
    record_attempt_finish(tmp_path, start, finished_ns=START + 2_000_000_000)
    with pytest.raises(ValueError, match="already finalized"):
        record_attempt_finish(tmp_path, start, finished_ns=START + 3_000_000_000)


def test_missing_start_invalidates_saved_result(tmp_path):
    start = record_attempt_start(tmp_path, S, SCHEDULE, START, 0)
    record_attempt_finish(tmp_path, start, finished_ns=START + 2_000_000_000)
    folder = tmp_path / "binance-usdm" / "forward-attempts" / S
    next(folder.glob("*.start.json")).unlink()
    with pytest.raises(ValueError, match="without original intent"):
        load_attempts(tmp_path, S)


def test_wrong_symbol_and_invalid_clock_cannot_write_intent(tmp_path):
    with pytest.raises(ValueError, match="non-BTC"):
        record_attempt_start(tmp_path, "BTCUSDT", SCHEDULE, START, 0)
    with pytest.raises(ValueError, match="invalid attempted"):
        record_attempt_start(tmp_path, S, SCHEDULE + 1, START, 0)
    with pytest.raises(ValueError, match="invalid attempted"):
        record_attempt_start(tmp_path, S, SCHEDULE, SCHEDULE - 1, 0)
    assert not list(tmp_path.rglob("*.start.json"))


def test_late_poll_and_wrong_minute_are_reported_not_reinterpreted(tmp_path):
    start = record_attempt_start(tmp_path, S, T, T + 15_000_000_000, 0)
    price = _price(tmp_path, START + 1_000_000_000)
    record_attempt_finish(
        tmp_path,
        start,
        finished_ns=START + 2_000_000_000,
        price_received_ns=price.received_ns,
        price_bar_end_ns=price.bar.end_ns,
    )
    report = coverage_from_attempts(load_attempts(tmp_path, S))
    assert report.late_attempts == 1
    assert report.wrong_minute_price == 1


def test_cli_collector_writes_both_intent_and_result(tmp_path, monkeypatch):
    from lab import bearish_hybrid_forward_collector as runner

    price = _price(tmp_path, START + 1_000_000_000)
    clock = iter((T + 5_000_000_000, START, START + 2_000_000_000))
    monkeypatch.setattr(runner.time, "time_ns", lambda: next(clock))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        runner, "capture_public_minute", lambda root, symbol, index: PublicMinuteCapture(price, None, None)
    )
    result = run_bounded_capture(tmp_path, S, 1)
    assert len(result) == 1
    entries = load_attempts(tmp_path, S)
    assert len(entries) == 1
    assert entries[0].finish.price_received_ns == price.received_ns
    assert coverage_from_attempts(entries).price_missing == 0


def test_unexpected_runtime_exception_still_records_terminal_abort(tmp_path, monkeypatch):
    from lab import bearish_hybrid_forward_collector as runner

    clock = iter((T + 5_000_000_000, START, START + 2_000_000_000))
    monkeypatch.setattr(runner.time, "time_ns", lambda: next(clock))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)

    def fail(root, symbol, index):
        raise ValueError("invalid source JSON")

    monkeypatch.setattr(runner, "capture_public_minute", fail)
    with pytest.raises(ValueError, match="invalid source JSON"):
        run_bounded_capture(tmp_path, S, 1)
    records = load_attempts(tmp_path, S)
    assert records[0].finish.aborted_error == "ValueError"
