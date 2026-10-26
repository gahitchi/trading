"""Offline original-byte capture integrity and causal frame continuity."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lab.markets.book_capture import BookCaptureError, verify_recorded_book_capture


def frame(action: str, sequence: int, previous: int, ms: int, *, instrument="BTC-USD-SWAP"):
    return {
        "arg": {"channel": "books", "instId": instrument},
        "action": action,
        "data": [
            {
                "ts": str(ms),
                "seqId": sequence,
                "prevSeqId": previous,
                "checksum": 0,
                "bids": [["100.0", "10.000", "0", "1"]],
                "asks": [["100.2", "10.000", "0", "1"]],
            }
        ],
    }


def records():
    return [
        {"received_ns": 1_000_000_001, "frame": frame("snapshot", 10, -1, 1000)},
        {"received_ns": 1_001_000_001, "frame": frame("update", 11, 10, 1001)},
    ]


def save(tmp_path: Path, items=None):
    data = records() if items is None else items
    raw = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in data).encode()
    path = tmp_path / "capture.ndjson"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_byte_bound_capture_accepts_only_reconstructable_seq_chain(tmp_path):
    path, digest = save(tmp_path)
    evidence = verify_recorded_book_capture(path, digest, "BTC-USD-SWAP")
    assert evidence.frame_count == 2
    assert evidence.first_sequence == 10
    assert evidence.last_sequence == 11
    assert evidence.first_received_ns == 1_000_000_001
    assert evidence.last_received_ns == 1_001_000_001
    assert evidence.final_book.best_ask == evidence.final_book.asks[0].price
    assert evidence.sha256 == digest
    assert not evidence.source_authenticated
    assert not evidence.feed_complete
    assert not evidence.market_data_execution_qualified
    assert not evidence.broker_execution_enabled


def test_offline_capture_cli_never_claims_exchange_authority(tmp_path):
    path, digest = save(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "lab.markets",
            "verify-book-capture",
            "--input",
            str(path),
            "--sha256",
            digest,
            "--instrument",
            "BTC-USD-SWAP",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=40,
    )
    data = json.loads(completed.stdout)
    assert data["frames_verified"] == 2
    assert data["sha256"] == digest
    assert data["first_sequence"] == 10
    assert data["last_sequence"] == 11
    assert not data["source_authenticated"]
    assert not data["feed_complete"]
    assert not data["market_data_execution_qualified"]
    assert not data["broker_execution_enabled"]
    assert not data["qualified_for_live_trading"]


def test_mutations_cannot_reuse_prior_recording_fingerprint(tmp_path):
    path, digest = save(tmp_path)
    path.write_bytes(path.read_bytes().replace(b'"seqId":11', b'"seqId":12'))
    with pytest.raises(BookCaptureError, match="SHA-256 mismatch"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP")


def test_bad_sequences_are_rejected_even_with_matching_fresh_digest(tmp_path):
    wrong = records()
    wrong[1]["frame"]["data"][0]["prevSeqId"] = 9
    path, digest = save(tmp_path, wrong)
    with pytest.raises(BookCaptureError, match="sequence"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP")
    future = records()
    future[1]["frame"]["data"][0]["ts"] = "1002"
    path, digest = save(tmp_path, future)
    with pytest.raises(BookCaptureError, match="sequence"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP")


def test_identity_bounds_and_source_credentials_cannot_be_claimed(tmp_path):
    path, digest = save(tmp_path)
    with pytest.raises(BookCaptureError, match="identity mismatch"):
        verify_recorded_book_capture(path, digest, "ETH-USD-SWAP")
    with pytest.raises(BookCaptureError, match="byte limit"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP", max_bytes=2)
    with pytest.raises(BookCaptureError, match="frame count"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP", max_frames=1)
    wrong = records()
    wrong[1]["claimed_authenticated"] = True
    path, digest = save(tmp_path, wrong)
    with pytest.raises(BookCaptureError, match="unexpected recording fields"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP")


def test_truncation_duplicate_json_keys_and_symlink_refused(tmp_path):
    path, digest = save(tmp_path)
    content = path.read_bytes()
    path.write_bytes(content[:-1])
    fresh = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(BookCaptureError, match="terminal newline"):
        verify_recorded_book_capture(path, fresh, "BTC-USD-SWAP")
    assert digest != fresh

    dup = b'{"received_ns":1000000001,"received_ns":1000000001,"frame":{}}\n'
    path.write_bytes(dup)
    with pytest.raises(BookCaptureError, match="ambiguous JSON"):
        verify_recorded_book_capture(path, hashlib.sha256(dup).hexdigest(), "BTC-USD-SWAP")

    link = tmp_path / "link.ndjson"
    link.symlink_to(path)
    with pytest.raises(BookCaptureError, match="symlink"):
        verify_recorded_book_capture(link, hashlib.sha256(dup).hexdigest(), "BTC-USD-SWAP")


def test_verified_capture_replays_as_read_only_quotes_through_existing_native_actor(tmp_path):
    """Original bytes -> validated book states -> existing native Nautilus DataActor."""
    from decimal import Decimal

    from lab.markets.domain import AssetClass, ContractKind, Instrument
    from lab.markets.nautilus_data import build_causal_tape
    from lab.markets.nautilus_instruments import LinearContractDefinition
    from lab.markets.nautilus_runtime import replay_native_observations

    path, digest = save(tmp_path)
    evidence = verify_recorded_book_capture(path, digest, "BTC-USD-SWAP", retain_books=True)
    assert len(evidence.recorded_books) == 2
    assert evidence.recorded_books[0].seq_id == 10
    assert evidence.recorded_books[1].seq_id == 11
    assert evidence.recorded_books[-1] == evidence.final_book
    assert not evidence.market_data_execution_qualified

    d = Decimal
    instrument = Instrument(
        "SIM", "BTC-USD-SWAP", AssetClass.CRYPTO, ContractKind.LINEAR_DERIVATIVE,
        "USD", d("0.1"), d("0.001"), d("0.001"), d("5"), multiplier=d("1"),
    )
    contract = LinearContractDefinition(
        instrument, "BTC", "USD", d("2"), d("5"), 0, 0, "offline_fixture",
    )
    tape = build_causal_tape(instrument, evidence.recorded_books)
    assert all(type(event).__name__ == "QuoteTick" for event in tape.events)
    assert tuple(event.ts_init for event in tape.events) == (
        1_000_000_001, 1_001_000_001,
    )
    first = replay_native_observations(contract, tape)
    second = replay_native_observations(contract, tape)
    assert first == second
    assert first.quote_observations == 2
    assert first.trade_observations == 0
    assert first.mark_observations == 0
    assert first.native_data_engine_used and first.event_integrity_verified
    assert first.clock_receipt_verified
    assert not first.broker_execution_enabled
    assert not first.strategies_qualified


def test_capture_retention_is_explicit_and_fails_closed_on_large_books(tmp_path):
    path, digest = save(tmp_path)
    ordinary = verify_recorded_book_capture(path, digest, "BTC-USD-SWAP")
    assert ordinary.recorded_books == ()
    with pytest.raises(BookCaptureError, match="explicit boolean"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP", retain_books=1)

    frames = [
        {"received_ns": 1_000_000_001, "frame": frame("snapshot", 10, -1, 1000)}
    ]
    frames.extend(
        {"received_ns": 1_000_000_001 + i * 1_000_000, "frame": frame("update", 10 + i, 9 + i, 1000 + i)}
        for i in range(1, 257)
    )
    path, digest = save(tmp_path, frames)
    # A large recording may still be checked without retaining the whole book.
    validated = verify_recorded_book_capture(path, digest, "BTC-USD-SWAP")
    assert validated.frame_count == 257
    assert validated.recorded_books == ()
    with pytest.raises(BookCaptureError, match="retention bound"):
        verify_recorded_book_capture(path, digest, "BTC-USD-SWAP", retain_books=True)
