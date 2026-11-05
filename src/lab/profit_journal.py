"""Crash-conservative, append-only accounting event journal for paper/research.

This is NOT an execution engine. Every append replays and validates the full
source-linked journal under a process lock. A torn write, broken hash chain,
duplicate settlement or missing UTC day-close prevents further mutation;
there is no automated truncation, replacement, or history reset.
The SHA-256 chain detects accidental changes but is NOT source authentication.
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from lab.persistence import atomic_json, file_lock, parse_json, read_json
from lab.profit_allocation import AllocationEvent, AllocationPolicy, DayClose, ProfitAllocationLedger

_VERSION = 1
_HEADER = "profit-allocation-header"
_ENTRY = "profit-allocation-event"
_HEAD = "profit-allocation-checkpoint"
_EVENT_FIELDS = {field.name for field in fields(AllocationEvent)}
_POLICY_FIELDS = {field.name for field in fields(AllocationPolicy)}


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sealed(value: dict) -> dict:
    return {**value, "sha256": _digest(value)}


def _verify_seal(value: dict, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected | {"sha256"}:
        raise ValueError("invalid journal record fields")
    given = value["sha256"]
    if not isinstance(given, str) or len(given) != 64 or given != _digest(
        {key: val for key, val in value.items() if key != "sha256"}
    ):
        raise ValueError("journal integrity digest mismatch")


def _policy_data(policy: AllocationPolicy) -> dict:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(policy).items()
    }


def _event_data(event: AllocationEvent) -> dict:
    raw = asdict(event)
    raw["timestamp"] = event.timestamp.isoformat()
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in raw.items()
    }


def _deserialize_event(raw: dict) -> AllocationEvent:
    if not isinstance(raw, dict) or set(raw) != _EVENT_FIELDS:
        raise ValueError("invalid journal event schema")
    if not isinstance(raw["timestamp"], str):
        raise ValueError("invalid event timestamp")
    try:
        stamp = datetime.fromisoformat(raw["timestamp"])
    except ValueError as exc:
        raise ValueError("invalid event timestamp") from exc
    return AllocationEvent(**{**raw, "timestamp": stamp})


def _read_records(path: Path) -> list[dict]:
    if path.is_symlink():
        raise ValueError("journal must not be a symbolic link")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("journal has an incomplete or missing final record")
    rows = []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n") or len(line) == 1:
            raise ValueError("journal contains a torn or empty line")
        try:
            record = parse_json(line.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ValueError("invalid or partially written journal JSON") from exc
        if not isinstance(record, dict):
            raise ValueError("journal record is not an object")
        rows.append(record)
    return rows


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        size = os.write(fd, view)
        if size <= 0:
            raise OSError("journal write made no progress")
        view = view[size:]


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ProfitJournal:
    """Immutable policy, serial writes and fail-closed full replay.

    Requires exclusive ownership of each economic source_id. The upstream
    fill/settlement reconciler must still authenticate the source and
    supply correct gross/fee/funding and released-mark measurements.
    """

    def __init__(self, path: Path, policy: AllocationPolicy, start_day: date):
        self.path = Path(path)
        self.policy = policy
        self.start_day = start_day
        # Policy and date are validated before publishing the journal header.
        ProfitAllocationLedger(policy, start_day)
        self._lock = self.path.with_name(self.path.name + ".lock")
        self._head = self.path.with_name(self.path.name + ".head.json")
        if self.path.is_symlink() or self._lock.is_symlink() or self._head.is_symlink():
            raise ValueError("journal, checkpoint and lock must not be symbolic links")
        with file_lock(self._lock):
            if not self.path.exists():
                if self._head.exists():
                    raise ValueError("journal is missing but its durable checkpoint exists")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                header = _sealed({
                    "version": _VERSION,
                    "type": _HEADER,
                    "policy": _policy_data(self.policy),
                    "start_day": self.start_day.isoformat(),
                })
                data = _canonical(header) + b"\n"
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    _write_all(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _sync_dir(self.path.parent)
                self._publish_head(sequence=0, last=header["sha256"], header=header["sha256"])
            self._load_unlocked()

    def _publish_head(self, *, sequence: int, last: str, header: str) -> None:
        # This separately fsynced head seals the journal suffix: truncating
        # a complete trailing record cannot silently lose a settled event.
        atomic_json(self._head, _sealed({
            "version": _VERSION,
            "type": _HEAD,
            "sequence": sequence,
            "last_sha256": last,
            "header_sha256": header,
        }))

    def _checkpoint_header_unlocked(self) -> str:
        # The current validated checkpoint already binds the header digest.
        # No additional reader opens external market input or source files.
        checkpoint = read_json(self._head)
        _verify_seal(checkpoint, {
            "version", "type", "sequence", "last_sha256", "header_sha256"
        })
        return checkpoint["header_sha256"]

    def _load_unlocked(self) -> tuple[ProfitAllocationLedger, int, str]:
        rows = _read_records(self.path)
        header = rows[0]
        _verify_seal(header, {"version", "type", "policy", "start_day"})
        if header["version"] != _VERSION or header["type"] != _HEADER:
            raise ValueError("unsupported accounting journal header")
        if not isinstance(header["policy"], dict) or set(header["policy"]) != _POLICY_FIELDS:
            raise ValueError("invalid journal allocation policy schema")
        if header["policy"] != _policy_data(self.policy) or header["start_day"] != self.start_day.isoformat():
            raise ValueError("journal was created for a different frozen policy or UTC start day")
        ledger = ProfitAllocationLedger(self.policy, self.start_day)
        previous = header["sha256"]
        for sequence, row in enumerate(rows[1:], start=1):
            _verify_seal(row, {"version", "type", "sequence", "previous_sha256", "event"})
            if row["version"] != _VERSION or row["type"] != _ENTRY:
                raise ValueError("unsupported journal event type or version")
            if type(row["sequence"]) is not int or row["sequence"] != sequence:
                raise ValueError("missing, duplicated or reordered journal sequence")
            if row["previous_sha256"] != previous:
                raise ValueError("broken journal predecessor hash chain")
            ledger.apply(_deserialize_event(row["event"]))
            previous = row["sha256"]
        if not self._head.exists():
            raise ValueError("journal durable checkpoint is missing")
        checkpoint = read_json(self._head)
        _verify_seal(checkpoint, {
            "version", "type", "sequence", "last_sha256", "header_sha256"
        })
        if (checkpoint["version"] != _VERSION or checkpoint["type"] != _HEAD
                or type(checkpoint["sequence"]) is not int
                or checkpoint["sequence"] != len(rows) - 1
                or checkpoint["last_sha256"] != previous
                or checkpoint["header_sha256"] != header["sha256"]):
            raise ValueError("journal/checkpoint mismatch: missing committed event or incomplete write")
        return ledger, len(rows) - 1, previous

    def replay(self) -> ProfitAllocationLedger:
        """Verify all historical events on every read. Do not expose a mutable cache."""
        with file_lock(self._lock):
            ledger, _, _ = self._load_unlocked()
            return ledger

    def append(self, event: AllocationEvent) -> DayClose | None:
        """Validate against the current replay before appending one fsynced line.

        If writing fails, no successful append is claimed. The next read
        checks the entire file; a partial line cannot be skipped or repaired
        automatically. A fully committed event is deduplicated by source_id.
        """
        with file_lock(self._lock):
            ledger, sequence, previous = self._load_unlocked()
            proposed = deepcopy(ledger)
            day = proposed.apply(event)
            record = _sealed({
                "version": _VERSION,
                "type": _ENTRY,
                "sequence": sequence + 1,
                "previous_sha256": previous,
                "event": _event_data(event),
            })
            raw = _canonical(record) + b"\n"
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
            try:
                _write_all(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            # A crash between the event append and this checkpoint update
            # must stop future writes until evidence is audited manually.
            self._publish_head(sequence=sequence + 1, last=record["sha256"], header=self._checkpoint_header_unlocked())
            return day

    def events(self) -> tuple[AllocationEvent, ...]:
        """Read only verified committed events for deterministic source reconciliation."""
        with file_lock(self._lock):
            self._load_unlocked()  # validates journal, chained events and head
            rows = _read_records(self.path)
            return tuple(_deserialize_event(row["event"]) for row in rows[1:])

    def append_batch(self, events: list[AllocationEvent]) -> list[DayClose]:
        """Commit a dependent set of economic events under ONE durable checkpoint.

        Intended for a fill's realized-cash entry and its complete
        post-fill position mark. The full batch must validate against the
        current replay before any bytes are written. Interrupted writes
        fail closed on next replay via the head sequence/hash mismatch.
        This is logical atomic visibility, not a claim of disk transaction
        rollback or broker acknowledgement.
        """
        if not isinstance(events, list) or not events:
            raise ValueError("journal batch must contain at least one event")
        with file_lock(self._lock):
            ledger, sequence, previous = self._load_unlocked()
            simulated = deepcopy(ledger)
            records: list[dict] = []
            closed: list[DayClose] = []
            chain = previous
            for event in events:
                result = simulated.apply(event)
                if result is not None:
                    closed.append(result)
                row = _sealed({
                    "version": _VERSION,
                    "type": _ENTRY,
                    "sequence": sequence + len(records) + 1,
                    "previous_sha256": chain,
                    "event": _event_data(event),
                })
                records.append(row)
                chain = row["sha256"]
            raw = b"".join(_canonical(row) + b"\n" for row in records)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
            try:
                _write_all(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._publish_head(
                sequence=sequence + len(records),
                last=chain,
                header=self._checkpoint_header_unlocked(),
            )
            return closed

    def report(self, through: date) -> dict:
        return self.replay().report(through)
