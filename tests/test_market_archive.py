"""Crash-resilient, independently verified compression tests (no network needed)."""

import fcntl
import gzip
import json
from pathlib import Path

import pytest

from lab.market_archive import ArchiveIntegrityError, archive_directory


def make_source(folder, name="public-session-0000.jsonl"):
    src = folder / name
    data = b"".join(
        json.dumps({"received_ns": x, "stream": "btcusdt@bookTicker", "data": {"u": x}}).encode() + b"\n"
        for x in range(100)
    )
    src.write_bytes(data)
    return src, data


def test_round_trip_preserves_source_and_verified_manifest(tmp_path):
    src, data = make_source(tmp_path)
    result = archive_directory(tmp_path)
    assert len(result) == 1
    assert result[0].records == 100
    assert result[0].original_bytes == len(data)
    assert src.read_bytes() == data
    assert gzip.decompress(result[0].path.read_bytes()) == data
    meta = json.loads(Path(str(result[0].path) + ".manifest.json").read_text())
    assert meta["records"] == 100 and meta["schema_version"] == 1
    assert archive_directory(tmp_path)[0] == result[0]


def test_prune_only_after_verified_and_idempotent(tmp_path):
    src, data = make_source(tmp_path)
    result = archive_directory(tmp_path, prune_verified=True)
    assert len(result) == 1 and result[0].pruned
    assert not src.exists()
    assert gzip.decompress(result[0].path.read_bytes()) == data
    again = archive_directory(tmp_path)
    assert len(again) == 1 and not again[0].pruned


def test_existing_corrupted_archive_blocks_prune(tmp_path):
    src, _ = make_source(tmp_path)
    archive = archive_directory(tmp_path)[0].path
    archive.write_bytes(b"not-gzip")
    with pytest.raises(ArchiveIntegrityError, match="invalid gzip"):
        archive_directory(tmp_path, prune_verified=True)
    assert src.exists()


def test_incomplete_gzip_partial_retried_from_untouched_source(tmp_path):
    src, data = make_source(tmp_path)
    partial = Path(str(src) + ".gz.partial")
    partial.write_bytes(b"incomplete compressed file")
    result = archive_directory(tmp_path)[0]
    assert gzip.decompress(result.path.read_bytes()) == data
    assert not partial.exists()


def test_missing_manifest_after_rename_is_recovered(tmp_path):
    src, data = make_source(tmp_path)
    archive = Path(str(src) + ".gz")
    archive.write_bytes(gzip.compress(data))
    result = archive_directory(tmp_path)
    assert len(result) == 1
    assert Path(str(archive) + ".manifest.json").exists()


def test_corrupted_or_mismatched_gzip_with_no_manifest_is_rejected(tmp_path):
    src, _ = make_source(tmp_path)
    archive = Path(str(src) + ".gz")
    archive.write_bytes(gzip.compress(b"other\n"))
    with pytest.raises(ArchiveIntegrityError, match="mismatched gzip"):
        archive_directory(tmp_path, prune_verified=True)
    assert src.exists()


def test_archive_does_not_touch_open_journal(tmp_path):
    (tmp_path / "market-session-0001.jsonl.open").write_text("still being written\n")
    assert archive_directory(tmp_path) == []
    assert (tmp_path / "market-session-0001.jsonl.open").exists()


def test_single_writer_lock_prevents_concurrent_archiver(tmp_path):
    make_source(tmp_path)
    with (tmp_path / ".capture.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already holds"):
            archive_directory(tmp_path)


def test_missing_newline_is_rejected_without_deleting_raw(tmp_path):
    src, data = make_source(tmp_path)
    src.write_bytes(data[:-1])
    with pytest.raises(ArchiveIntegrityError, match="terminating newline"):
        archive_directory(tmp_path, prune_verified=True)
    assert src.exists()


def test_recovery_manifest_source_mismatch_blocks_prune(tmp_path):
    src, _ = make_source(tmp_path)
    archive_directory(tmp_path)
    src.write_bytes(b'{"modified":true}\n')
    with pytest.raises(ArchiveIntegrityError, match="source differs"):
        archive_directory(tmp_path, prune_verified=True)
    assert src.exists()
