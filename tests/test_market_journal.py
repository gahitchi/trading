"""Regression tests for durable market-event journal and crash recovery."""

import json
from pathlib import Path

import pytest

from lab.market_journal import JournalWriter, recover_directory, recover_open_segment


def event(n=1):
    return {"received_ns": n, "stream": "btcusdt@bookTicker", "data": {"u": n}}


def test_rotation_is_atomic_and_preserves_all_events(tmp_path):
    now = [0.0]
    with JournalWriter(tmp_path, "public", "session", clock=lambda: now[0]) as writer:
        writer.append(event(1))
        now[0] = 0.5
        writer.append(event(2))
        now[0] = 10.5
        writer.append(event(3))
    files = sorted(tmp_path.glob("*.jsonl"))
    assert len(files) == 2
    assert not list(tmp_path.glob("*.open"))
    records = [json.loads(line) for file in files for line in file.read_text().splitlines()]
    assert [record["received_ns"] for record in records] == [1, 2, 3]


def test_simulated_sigkill_recovers_unfinalized_file(tmp_path):
    writer = JournalWriter(tmp_path, "market", "session")
    writer.append(event())
    # Emulate process termination: file is closed without the finalization/rename path.
    writer._file.close()
    recovered = recover_directory(tmp_path)
    assert len(recovered) == 1
    assert recovered[0].records == 1
    assert recovered[0].discarded_bytes == 0
    assert json.loads(recovered[0].path.read_text()) == event()
    assert not list(tmp_path.glob("*.open"))
    assert recover_directory(tmp_path) == []


def test_only_trailing_incomplete_line_is_dropped_and_logged(tmp_path):
    p = tmp_path / "public-session-0000.jsonl.open"
    complete = json.dumps(event()).encode() + b"\n"
    tail = b'{"received_ns":2,"stream":'
    p.write_bytes(complete + tail)
    report = recover_open_segment(p)
    assert report.records == 1
    assert report.discarded_bytes == len(tail)
    assert report.path.read_bytes() == complete
    metadata = json.loads(Path(str(report.path) + ".recovery.json").read_text())
    assert metadata["discarded_bytes"] == len(tail)
    assert metadata["sha256"] == report.sha256


def test_invalid_complete_row_fails_closed(tmp_path):
    p = tmp_path / "market-session-0000.jsonl.open"
    original = json.dumps(event()).encode() + b"\n{invalid}\n"
    p.write_bytes(original)
    with pytest.raises(ValueError, match="corrupt complete"):
        recover_open_segment(p)
    assert p.read_bytes() == original
    assert not list(tmp_path.glob("*.recovered.jsonl"))


def test_recovery_does_not_clobber_existing_file(tmp_path):
    p = tmp_path / "market-session-0000.jsonl.open"
    p.write_text(json.dumps(event()) + "\n")
    dest = tmp_path / "market-session-0000.recovered.jsonl"
    dest.write_text("existing")
    with pytest.raises(FileExistsError):
        recover_open_segment(p)
    assert dest.read_text() == "existing"
    assert p.exists()


def test_invalid_event_fails_before_open(tmp_path):
    with JournalWriter(tmp_path, "market", "session") as journal:
        with pytest.raises(ValueError, match="received_ns"):
            journal.append({"received_ns": -1, "stream": "x", "data": {}})
        with pytest.raises(ValueError, match="data"):
            journal.append({"received_ns": 1, "stream": "x", "data": []})
    assert not list(tmp_path.glob("*.jsonl.open"))


def test_invalid_category_and_recovery_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="category/session"):
        JournalWriter(tmp_path, "../escape", "session")
    with pytest.raises(ValueError, match="expected"):
        recover_open_segment(tmp_path / "unexpected.jsonl")
