"""Tamper-evident, offline-only ingestion of operator-recorded WS book frames.

A SHA-256 check binds the exact on-disk NDJSON bytes to a caller-supplied
expected digest. The recorded snapshot and every incremental frame are
reconstructed with strict causal sequence validation. This is evidence of
content integrity and local consistency, NEVER authentication of the alleged
exchange, completeness before/after capture, or permission to execute.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from lab.markets.book_sequence import SequencedBook, apply_recorded_book_frame

_MAX_BYTES = 4 * 1024 * 1024
_MAX_FRAMES = 10_000
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BookCaptureError(ValueError):
    """A recorded book session is missing, altered or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class BookCaptureEvidence:
    instrument_id: str
    sha256: str
    frame_count: int
    first_received_ns: int
    last_received_ns: int
    first_sequence: int
    last_sequence: int
    final_book: SequencedBook
    source: str = "unverified_operator_recorded_ws_ndjson"
    source_authenticated: bool = False
    feed_complete: bool = False
    market_data_execution_qualified: bool = False
    broker_execution_enabled: bool = False


def _reject_nonnative_number(_: str):
    raise BookCaptureError("JSON floats and nonfinite numbers are forbidden in recorded frames")


def _unique_keys(items: list[tuple[str, object]]) -> dict:
    parsed = {}
    for key, value in items:
        if key in parsed:
            raise BookCaptureError("duplicate JSON key in recorded market data")
        parsed[key] = value
    return parsed


def verify_recorded_book_capture(
    path: Path,
    expected_sha256: str,
    instrument_id: str,
    *,
    max_bytes: int = _MAX_BYTES,
    max_frames: int = _MAX_FRAMES,
) -> BookCaptureEvidence:
    """Validate a recorded session without networking, trusting or replaying orders.

    The caller must obtain the expected digest independently of this file:
    re-hashing a compromised file and supplying its digest provides no
    external authenticity. Each row is exactly:
        {"received_ns": <int>, "frame": <OKX standard books WS frame>}
    Source receipt times are locally recorded UTC nanoseconds, not exchange
    timestamps. An external qualified feed requires independent verification.
    """
    if not isinstance(path, Path) or path.is_symlink():
        raise BookCaptureError("recording must be a regular path, not a symlink")
    if type(expected_sha256) is not str or not _SHA256.fullmatch(expected_sha256):
        raise BookCaptureError("expected SHA-256 must be lowercase hex")
    if type(instrument_id) is not str or not re.fullmatch(r"[A-Z0-9_-]{3,80}", instrument_id):
        raise BookCaptureError("invalid expected instrument identity")
    if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES:
        raise BookCaptureError("invalid bounded capture byte limit")
    if type(max_frames) is not int or not 0 < max_frames <= _MAX_FRAMES:
        raise BookCaptureError("invalid bounded capture frame limit")

    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > max_bytes:
            raise BookCaptureError("capture must be a bounded nonempty regular file")
        with os.fdopen(descriptor, "rb") as file:
            descriptor = None
            raw = file.read(max_bytes + 1)
            after = os.fstat(file.fileno())
        if (
            len(raw) > max_bytes
            or before.st_size != len(raw)
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        ):
            raise BookCaptureError("recorded market data changed during reading or exceeded size limit")
    except OSError as exc:
        raise BookCaptureError("recorded market data is not readable as a regular file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    observed_hash = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(expected_sha256, observed_hash):
        raise BookCaptureError("recorded market data SHA-256 mismatch")

    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BookCaptureError("recorded market data is not UTF-8") from exc
    if not content.endswith("\n"):
        raise BookCaptureError("truncated recorded session: terminal newline missing")

    state = None
    first = None
    count = 0
    for line in content.splitlines():
        if not line:
            raise BookCaptureError("blank recording row is not a market event")
        count += 1
        if count > max_frames:
            raise BookCaptureError("capture exceeds bounded event count")
        try:
            item = json.loads(
                line,
                object_pairs_hook=_unique_keys,
                parse_float=_reject_nonnative_number,
                parse_constant=_reject_nonnative_number,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BookCaptureError("invalid or ambiguous JSON recording") from exc
        if type(item) is not dict or set(item) != {"received_ns", "frame"}:
            raise BookCaptureError("unexpected recording fields or missing receipt time")
        try:
            state = apply_recorded_book_frame(state, item["frame"], received_ns=item["received_ns"])
        except (TypeError, ValueError) as exc:
            raise BookCaptureError("invalid, out-of-order or incomplete WS book sequence") from exc
        if state.instrument_id != instrument_id:
            raise BookCaptureError("recorded instrument identity mismatch")
        if first is None:
            first = state

    if not count or first is None or state is None:
        raise BookCaptureError("empty book recording")
    return BookCaptureEvidence(
        instrument_id=instrument_id,
        sha256=observed_hash,
        frame_count=count,
        first_received_ns=first.received_ns,
        last_received_ns=state.received_ns,
        first_sequence=first.seq_id,
        last_sequence=state.seq_id,
        final_book=state,
    )
