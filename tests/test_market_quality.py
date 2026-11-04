"""Offline data-quality gates for captured public market journals."""

from lab.market_archive import archive_directory
from lab.market_journal import JournalWriter, recover_directory
from lab.market_quality import audit_directory


def status(writer, ns, category, event):
    writer.append(
        {"received_ns": ns, "stream": "collector.status", "data": {"category": category, "event": event}}
    )


def market_event(writer, ns, category, identifier):
    if category == "public":
        stream, data = "btcusdt@bookTicker", {"u": identifier}
    else:
        stream, data = "btcusdt@aggTrade", {"a": identifier}
    writer.append({"received_ns": ns, "stream": stream, "data": data})


def make_fault_recording(path, category="public", *, last_epoch=True):
    with JournalWriter(path, category, "20261104T123456Z") as writer:
        status(writer, 1_000_000, category, "connected")
        market_event(writer, 1_000_001, category, 11)
        status(writer, 1_000_002, category, "fault_injected_disconnect")
        status(writer, 1_000_003, category, "disconnected")
        status(writer, 1_500_003, category, "connected")
        if last_epoch:
            market_event(writer, 1_500_005, category, 12)


def test_fault_injection_report_proves_market_events_after_reconnect(tmp_path):
    make_fault_recording(tmp_path, "public")
    make_fault_recording(tmp_path, "market")
    result = audit_directory(tmp_path, require_reconnect=True)
    assert result["ok"], result["issues"]
    assert result["market_events"] == 4
    for category in ("public", "market"):
        item = result["categories"][category]
        assert item["connection_epoch_events"] == [1, 1]
        assert item["reconnect_gaps"] == [{"gap_ms": 500.0, "closed": True}]
        assert item["statuses"]["fault_injected_disconnect"] == 1


def test_missing_post_reconnect_data_fails_gate(tmp_path):
    make_fault_recording(tmp_path, "public", last_epoch=False)
    result = audit_directory(tmp_path, require_reconnect=True)
    assert not result["ok"]
    assert any("no market events after reconnect" in item for item in result["issues"])


def test_nonincreasing_event_ids_are_reported(tmp_path):
    with JournalWriter(tmp_path, "public", "session") as writer:
        status(writer, 1, "public", "connected")
        market_event(writer, 2, "public", 20)
        market_event(writer, 3, "public", 20)
    result = audit_directory(tmp_path)
    assert not result["ok"]
    assert any("nonincreasing ID" in item for item in result["issues"])


def test_archives_can_be_audited_after_verified_raw_pruning(tmp_path):
    make_fault_recording(tmp_path, "public")
    make_fault_recording(tmp_path, "market")
    archived = archive_directory(tmp_path, prune_verified=True)
    assert len(archived) == 2
    assert not list(tmp_path.glob("*.jsonl"))
    result = audit_directory(tmp_path, require_reconnect=True)
    assert result["ok"], result["issues"]


def test_interrupted_segment_blocks_clean_verdict(tmp_path):
    make_fault_recording(tmp_path, "public")
    (tmp_path / "public-20261104T123456Z-0001.jsonl.open").write_bytes(b"partial")
    result = audit_directory(tmp_path)
    assert not result["ok"]
    assert result["unfinished_segments"]


def test_recovered_segment_requires_verified_manifest(tmp_path):
    with JournalWriter(tmp_path, "market", "session") as writer:
        status(writer, 1, "market", "connected")
        market_event(writer, 2, "market", 1)
    raw = tmp_path / "market-session-0000.jsonl"
    aborted = tmp_path / "market-session-0001.jsonl.open"
    aborted.write_text('{"received_ns":3,"stream":"btcusdt@aggTrade","data":{"a":2}}\n')
    restored = recover_directory(tmp_path)
    assert len(restored) == 1
    ok = audit_directory(tmp_path)
    assert ok["ok"], ok["issues"]
    restored[0].path.with_name(restored[0].path.name + ".recovery.json").unlink()
    bad = audit_directory(tmp_path)
    assert not bad["ok"]
    assert any("missing recovery manifest" in issue for issue in bad["issues"])


def test_empty_or_missing_directory_never_passes(tmp_path):
    assert not audit_directory(tmp_path)["ok"]
    assert not audit_directory(tmp_path, require_reconnect=True)["ok"]
