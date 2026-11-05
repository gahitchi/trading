"""Durable, source-linked *paper* evidence before accounting reconciliation.

The source is a supervised sandbox/replay producer, not an authenticated
exchange. SHA-256 detects accidental edits, not a malicious rewrite of both
the archive and checkpoint. A source archive append is committed *before*
calling the separate accounting journal, so recovery can replay an archived
suffix after a crash. Never submit orders or silently repair torn records.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from lab.persistence import atomic_json, file_lock, parse_json, read_json
from lab.position_reconcile import (
    CashAdjustment,
    MarkQuote,
    PositionMark,
    SimulatedFill,
    compile_source,
    reconcile_to_journal,
)
from lab.profit_allocation import AllocationPolicy, ProfitAllocationLedger
from lab.profit_journal import ProfitJournal

_VERSION = 1
_MAX_REPORT_BYTES = 1_000_000
_ORIGINS = {"nautilus-sandbox", "offline-replay"}
_EVENT_TYPES = {
    "fill": SimulatedFill,
    "mark": PositionMark,
    "cash": CashAdjustment,
}


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _hash(obj: dict) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def _seal(obj: dict) -> dict:
    return {**obj, "sha256": _hash(obj)}


def _check_seal(obj: object, expected: set[str]) -> None:
    if not isinstance(obj, dict) or set(obj) != expected | {"sha256"}:
        raise ValueError("paper evidence schema mismatch")
    digest = obj["sha256"]
    if not isinstance(digest, str) or digest != _hash({k: v for k, v in obj.items() if k != "sha256"}):
        raise ValueError("paper evidence digest mismatch")


def _utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("paper evidence time must be UTC aware")


def _timestamp(text: str) -> datetime:
    if not isinstance(text, str):
        raise ValueError("paper evidence timestamp must be a string")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("invalid paper evidence timestamp") from exc
    _utc(result)
    return result


def _primitive(value):
    if isinstance(value, datetime):
        _utc(value)
        return value.isoformat()
    if is_dataclass(value):
        return _primitive(asdict(value))
    if isinstance(value, dict):
        return {k: _primitive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_primitive(v) for v in value]
    return value


def paper_report(
    event: SimulatedFill | PositionMark | CashAdjustment, *, origin: str, captured_at: datetime, raw_receipt: dict
) -> dict:
    """Wrap a normalized paper event with its *unverified* original receipt.

    The producer must supply the original report and independent mark quotes,
    not reconstruct quote prices from a fill. Receipt source_id must match
    the normalized event for a deterministic chain of custody.
    """
    if type(event) not in _EVENT_TYPES.values():
        raise ValueError("unsupported paper evidence event")
    kind = next(k for k, cls in _EVENT_TYPES.items() if type(event) is cls)
    return {
        "origin": origin,
        "captured_at": _primitive(captured_at),
        "kind": kind,
        "event": _primitive(event),
        "raw_receipt": raw_receipt,
    }


def _decode(report: object, origin: str):
    if not isinstance(report, dict) or set(report) != {"origin", "captured_at", "kind", "event", "raw_receipt"}:
        raise ValueError("invalid paper report envelope")
    if report["origin"] != origin:
        raise ValueError("paper evidence origin mismatch")
    observed = _timestamp(report["captured_at"])
    kind = report["kind"]
    if not isinstance(kind, str) or kind not in _EVENT_TYPES:
        raise ValueError("unsupported paper evidence event kind")
    body = report["event"]
    cls = _EVENT_TYPES[kind]
    expected = {field.name for field in fields(cls)}
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError("invalid normalized paper event fields")
    body = body.copy()
    body["timestamp"] = _timestamp(body["timestamp"])
    if kind in {"fill", "mark"}:
        quotes = body["marks"]
        if not isinstance(quotes, dict):
            raise ValueError("invalid paper mark sources")
        parsed = {}
        for symbol, quote in quotes.items():
            if (
                not isinstance(symbol, str)
                or not isinstance(quote, dict)
                or set(quote) != {"price", "observed_at", "source_id"}
            ):
                raise ValueError("invalid paper mark quote")
            parsed[symbol] = MarkQuote(
                price=quote["price"],
                observed_at=_timestamp(quote["observed_at"]),
                source_id=quote["source_id"],
            )
        body["marks"] = parsed
    if kind == "mark" and type(body["close_day"]) is not bool:
        raise ValueError("close_day must be a boolean")
    record = cls(**body)
    raw = report["raw_receipt"]
    if not isinstance(raw, dict) or raw.get("source_id") != record.source_id:
        raise ValueError("raw receipt must identify the same source event")
    if not isinstance(record.source_id, str):
        raise ValueError("source identity is required")
    if observed < record.timestamp:
        raise ValueError("paper receipt cannot precede its source event")
    return record


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("paper archive write made no progress")
        view = view[count:]


class PaperEvidenceArchive:
    """One-account immutable paper receipt log with a durable suffix checkpoint.

    Every append validates the entire existing source against the frozen
    economic policy before writing. This deliberately is not a fast path.
    """

    def __init__(self, path: Path, policy: AllocationPolicy, start_day: date, *, origin: str = "nautilus-sandbox"):
        if origin not in _ORIGINS:
            raise ValueError("only named simulated paper evidence sources are allowed")
        ProfitAllocationLedger(policy, start_day)
        self.path = Path(path)
        self.policy = policy
        self.start_day = start_day
        self.origin = origin
        self._head = self.path.with_name(self.path.name + ".head.json")
        self._lock = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_paths()
        with file_lock(self._lock):
            if not self.path.exists():
                if self._head.exists():
                    raise ValueError("paper archive missing but checkpoint present")
                header = _seal(self._header())
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                try:
                    _write_all(fd, _canonical(header) + b"\n")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _sync_directory(self.path.parent)
                self._publish_head(0, header["sha256"], header["sha256"])
            self._read_locked()

    def _check_paths(self) -> None:
        if any(p.is_symlink() for p in (self.path, self._head, self._lock)):
            raise ValueError("paper evidence archive paths must not be symlinks")

    def _header(self) -> dict:
        return {
            "version": _VERSION,
            "type": "paper-evidence-header",
            "origin": self.origin,
            "start_day": self.start_day.isoformat(),
            "policy": _primitive(self.policy),
        }

    def _publish_head(self, sequence: int, last: str, header: str) -> None:
        atomic_json(
            self._head,
            _seal(
                {
                    "version": _VERSION,
                    "sequence": sequence,
                    "last_sha256": last,
                    "header_sha256": header,
                }
            ),
        )

    def _read_locked(self) -> tuple[list[dict], list, str, str]:
        self._check_paths()
        raw = self.path.read_bytes()
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("paper source archive incomplete or torn")
        try:
            records = [parse_json(line.decode("utf-8")) for line in raw.splitlines()]
        except (ValueError, UnicodeError, TypeError) as exc:
            raise ValueError("corrupt paper evidence JSON") from exc
        header = records[0]
        _check_seal(header, set(self._header()))
        if {k: v for k, v in header.items() if k != "sha256"} != self._header():
            raise ValueError("paper source archive was created for another policy or origin")
        last = header["sha256"]
        receipts = []
        events = []
        for index, record in enumerate(records[1:], start=1):
            _check_seal(record, {"version", "type", "sequence", "previous_sha256", "report"})
            if (
                record["version"] != _VERSION
                or record["type"] != "paper-evidence-record"
                or type(record["sequence"]) is not int
                or record["sequence"] != index
                or record["previous_sha256"] != last
            ):
                raise ValueError("paper evidence sequence or predecessor mismatch")
            report = record["report"]
            events.append(_decode(report, self.origin))
            receipts.append(report)
            last = record["sha256"]
        compile_source(self.policy, self.start_day, events)
        if not self._head.exists():
            raise ValueError("paper source checkpoint is missing")
        checkpoint = read_json(self._head)
        _check_seal(checkpoint, {"version", "sequence", "last_sha256", "header_sha256"})
        if (
            checkpoint["version"] != _VERSION
            or type(checkpoint["sequence"]) is not int
            or checkpoint["sequence"] != len(events)
            or checkpoint["last_sha256"] != last
            or checkpoint["header_sha256"] != header["sha256"]
        ):
            raise ValueError("paper archive/checkpoint mismatch: interrupted or missing evidence")
        return receipts, events, last, header["sha256"]

    def append(self, report: dict) -> None:
        encoded = _canonical(report)
        if len(encoded) > _MAX_REPORT_BYTES:
            raise ValueError("paper receipt exceeds bounded evidence size")
        # Reject JSON-incompatible types and nonfinite input before touching disk.
        canonical_report = parse_json(encoded.decode("utf-8"))
        source = _decode(canonical_report, self.origin)
        with file_lock(self._lock):
            _receipts, events, last, header = self._read_locked()
            compile_source(self.policy, self.start_day, [*events, source])
            record = _seal(
                {
                    "version": _VERSION,
                    "type": "paper-evidence-record",
                    "sequence": len(events) + 1,
                    "previous_sha256": last,
                    "report": canonical_report,
                }
            )
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                _write_all(fd, _canonical(record) + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            # A crash between append and checkpoint fails closed on restart.
            self._publish_head(len(events) + 1, record["sha256"], header)

    def receipts(self) -> tuple[dict, ...]:
        with file_lock(self._lock):
            receipts, _events, _last, _header = self._read_locked()
        return tuple(receipts)

    def source_events(self) -> tuple[SimulatedFill | PositionMark | CashAdjustment, ...]:
        with file_lock(self._lock):
            _receipts, events, _last, _header = self._read_locked()
        return tuple(events)

    def reconcile(self, journal: ProfitJournal) -> dict:
        """Replay the verified full source into the economic journal.

        An archived suffix may be replayed after a previous crash between
        source archival and economic posting. An economic journal ahead of
        or inconsistent with the evidence fails closed, never self-repairs.
        """
        if journal.policy != self.policy or journal.start_day != self.start_day:
            raise ValueError("paper source and profit journal policies do not match")
        return reconcile_to_journal(journal, list(self.source_events()))
