"""Portable, paper-only market-event journal and conservative crash recovery.

No exchange client or NautilusTrader dependency. One writer per (stream, session).
A committed segment is .jsonl; an interrupted one is .jsonl.open.
Completion requires fsync(file), rename, and fsync(directory). fsync on a
regular interval bounds kernel durability exposure, but does not make receipt
of WebSocket messages lossless or guarantee power-loss safety on Android.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_record(record: object) -> dict:
    if not isinstance(record, dict):
        raise ValueError("market event must be a JSON object")
    received = record.get("received_ns")
    if isinstance(received, bool) or not isinstance(received, int) or received <= 0:
        raise ValueError("received_ns must be a positive integer")
    if not isinstance(record.get("stream"), str) or not record["stream"]:
        raise ValueError("stream must be a nonempty string")
    if not isinstance(record.get("data"), dict):
        raise ValueError("data must be an object")
    return record


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON literal: {value}")


class JournalWriter:
    """Single-threaded, unbuffered JSONL writer with time rotation and periodic fsync."""

    def __init__(
        self,
        directory: Path,
        category: str,
        session: str,
        *,
        rotate_seconds: float = 10.0,
        sync_seconds: float = 1.0,
        clock=time.monotonic,
    ) -> None:
        if any(not _NAME.fullmatch(n) for n in (category, session)):
            raise ValueError("category/session may contain only letters, numbers, '-' and '_'")
        if rotate_seconds <= 0 or sync_seconds <= 0:
            raise ValueError("rotation and synchronization intervals must be positive")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.category = category
        self.session = session
        self.rotate_seconds = rotate_seconds
        self.sync_seconds = sync_seconds
        self.clock = clock
        self.sequence = 0
        self._file = None
        self._path: Path | None = None
        self._opened_at = 0.0
        self._sync_at = 0.0

    def _open(self, now: float) -> None:
        path = self.directory / f"{self.category}-{self.session}-{self.sequence:04d}.jsonl.open"
        # xb makes collisions fail closed; never silently overwrite an existing capture.
        self._file = path.open("xb", buffering=0)
        self._path = path
        self.sequence += 1
        self._opened_at = now
        self._sync_at = now + self.sync_seconds

    def append(self, record: dict) -> None:
        _validate_record(record)
        raw = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        now = self.clock()
        if self._file is not None and now - self._opened_at >= self.rotate_seconds:
            self.close()
        if self._file is None:
            self._open(now)
        self._file.write(raw)
        if now >= self._sync_at:
            os.fsync(self._file.fileno())
            self._sync_at = now + self.sync_seconds

    def close(self) -> None:
        if self._file is None:
            return
        f, path = self._file, self._path
        os.fsync(f.fileno())
        f.close()
        self._file = None
        self._path = None
        committed = path.with_suffix("")
        if committed.exists():
            raise FileExistsError(committed)
        os.replace(path, committed)
        _sync_directory(self.directory)

    def __enter__(self) -> JournalWriter:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


@dataclass(frozen=True)
class RecoveryReport:
    path: Path
    records: int
    original_bytes: int
    discarded_bytes: int
    sha256: str


def recover_open_segment(path: Path) -> RecoveryReport:
    """Recover newline-complete records; fail closed on corrupt complete lines.

    One incomplete trailing line may be removed. The sidecar records how many
    bytes were discarded. A corrupt line before EOF is never skipped.
    """
    path = Path(path)
    if not path.name.endswith(".jsonl.open"):
        raise ValueError("expected a .jsonl.open journal")
    dest = path.with_name(path.name.removesuffix(".jsonl.open") + ".recovered.jsonl")
    manifest = dest.with_name(dest.name + ".recovery.json")
    if dest.exists() or manifest.exists():
        raise FileExistsError(f"recovery destination already exists: {dest}")

    valid_end = 0
    records = 0
    with path.open("rb") as src:
        for line in src:
            if not line.endswith(b"\n"):
                break  # an incomplete final record, not evidence of a valid event
            try:
                _validate_record(json.loads(line, parse_constant=_reject_constant))
            except (ValueError, TypeError, UnicodeDecodeError) as exc:
                raise ValueError(f"corrupt complete record in {path}, line {records + 1}: {exc}") from exc
            valid_end = src.tell()
            records += 1

    original_bytes = path.stat().st_size
    discarded = original_bytes - valid_end
    digest = hashlib.sha256()
    with path.open("r+b") as f:
        if discarded:
            f.truncate(valid_end)
        f.seek(0)
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
        os.fsync(f.fileno())

    os.replace(path, dest)
    _sync_directory(path.parent)
    details = {
        "records": records,
        "original_bytes": original_bytes,
        "discarded_bytes": discarded,
        "sha256": digest.hexdigest(),
        "status": "recovered_after_interruption",
    }
    temp = manifest.with_name(manifest.name + ".tmp")
    with temp.open("x") as f:
        json.dump(details, f, sort_keys=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, manifest)
    _sync_directory(path.parent)
    return RecoveryReport(dest, records, original_bytes, discarded, digest.hexdigest())


def recover_directory(directory: Path) -> list[RecoveryReport]:
    return [recover_open_segment(path) for path in sorted(Path(directory).glob("*.jsonl.open"))]
