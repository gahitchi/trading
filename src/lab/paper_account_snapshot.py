"""Research-only paper account snapshots bound to durable sandbox order receipts.

This is an *independently supplied* account snapshot interface, not an
authenticated exchange statement. Positions are checked against a proven
flat-start, exclusive-scope fill prefix; cash is checked for internal
arithmetic only. Open-order membership is partial: unknown order state,
funding, true equity and margin always block risk qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lab.persistence import atomic_json, file_lock, parse_json, read_json
from lab.sandbox_capture import PaperOrderEventCapture

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,110}\Z")
_CURRENCY = re.compile(r"[A-Z][A-Z0-9]{2,7}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = 1
_TERMINAL = {"OrderRejected", "OrderDenied", "OrderCanceled", "OrderExpired"}
_ZERO = Decimal(0)


@dataclass(frozen=True)
class PaperAccountSnapshot:
    snapshot_id: str
    source_id: str
    source_kind: str  # sandbox-cache, a caller-supplied, UNVERIFIED identity
    account_id: str
    captured_ns: int
    capture_count: int
    capture_sha256: str
    started_flat: bool
    exclusive_scope: bool
    complete_snapshot: bool
    positions: dict[str, str]  # complete signed quantities
    open_orders: dict[str, str]  # client order ID -> positive remaining quantity
    balances: dict[str, dict[str, str]]  # currency -> total / available / locked CASH


def _decimal(value: object, name: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an exact decimal string")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not decimal.is_finite() or (nonnegative and decimal < 0):
        raise ValueError(f"{name} must be finite and valid")
    return decimal


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def _seal(value: dict) -> dict:
    return {**value, "sha256": hashlib.sha256(_canonical(value)).hexdigest()}


def _check_seal(row: object, expected: set[str]) -> None:
    if not isinstance(row, dict) or set(row) != expected | {"sha256"}:
        raise ValueError("paper account snapshot record schema mismatch")
    digest = row["sha256"]
    if not isinstance(digest, str) or digest != _seal({k: v for k, v in row.items() if k != "sha256"})["sha256"]:
        raise ValueError("paper account snapshot integrity digest mismatch")


def _check_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("paper account snapshot write made no progress")
        view = view[written:]


def validate_snapshot(
    capture: PaperOrderEventCapture, snapshot: PaperAccountSnapshot, account_id: str
) -> dict:
    """Verify a snapshot against its exact, cryptographically bound fill prefix.

    Requires *externally established* flat start and exclusive account scope.
    These booleans are assertions from the source, NOT independent evidence.
    Open-order absence and cash equity cannot be verified from fill events.
    """
    if not isinstance(snapshot, PaperAccountSnapshot):
        raise ValueError("paper account snapshot type is invalid")
    for value, label in (
        (snapshot.snapshot_id, "snapshot ID"),
        (snapshot.source_id, "source ID"),
        (snapshot.account_id, "snapshot account"),
    ):
        _check_id(value, label)
    if snapshot.account_id != account_id:
        raise ValueError("snapshot account identity mismatch")
    if snapshot.source_kind != "sandbox-cache":
        raise ValueError("unrecognized or unauthenticated snapshot producer")
    if any(type(v) is not bool or not v for v in (
        snapshot.started_flat, snapshot.exclusive_scope, snapshot.complete_snapshot
    )):
        raise ValueError("flat-start, exclusive-scope and complete-snapshot assertions required")
    if type(snapshot.captured_ns) is not int or snapshot.captured_ns <= 0:
        raise ValueError("invalid snapshot capture timestamp")
    if type(snapshot.capture_count) is not int or snapshot.capture_count < 0:
        raise ValueError("invalid snapshot receipt count")
    if not isinstance(snapshot.capture_sha256, str) or not _SHA.fullmatch(snapshot.capture_sha256):
        raise ValueError("invalid snapshot source checkpoint hash")

    prefix, digest = capture.evidence_prefix(snapshot.capture_count)
    if digest != snapshot.capture_sha256:
        raise ValueError("snapshot/capture hash mismatch: different evidence prefix")
    # A receipt already seen when the snapshot was taken cannot be omitted
    # from its declared complete prefix, even if it was archived later.
    later = capture.receipts()[snapshot.capture_count:]
    if any(row["received_ns"] <= snapshot.captured_ns for row in later):
        raise ValueError("snapshot omitted an earlier received order event")
    if prefix and snapshot.captured_ns < max(r["received_ns"] for r in prefix):
        raise ValueError("snapshot predates its declared execution evidence")

    if not isinstance(snapshot.positions, dict) or not isinstance(snapshot.open_orders, dict):
        raise ValueError("snapshot positions and orders must be complete objects")
    if not isinstance(snapshot.balances, dict) or not snapshot.balances:
        raise ValueError("snapshot requires explicit cash balances")
    if any(symbol != capture.instrument_id for symbol in snapshot.positions):
        raise ValueError("snapshot includes instrument outside captured scope")
    if any(not isinstance(sym, str) for sym in snapshot.positions):
        raise ValueError("invalid snapshot position key")
    positions = {key: _decimal(value, "position quantity") for key, value in snapshot.positions.items()}
    filled = _ZERO
    trade_ids: set[tuple[str, str]] = set()
    known_orders: set[str] = set()
    last_state: dict[str, str] = {}
    for row in prefix:
        order_id = row["client_order_id"]
        known_orders.add(order_id)
        last_state[order_id] = row["kind"]
        if row["kind"] == "OrderFillVoided":
            raise ValueError("voided order fills require independent corrections")
        if row["kind"] != "OrderFilled":
            continue
        if row["account_id"] != account_id:
            raise ValueError("fill account does not match independent snapshot account")
        key = (row["account_id"], row["trade_id"])
        if key in trade_ids:
            raise ValueError("duplicate or restated source trade")
        trade_ids.add(key)
        amount = _decimal(row["last_qty"], "source fill quantity")
        if amount <= 0:
            raise ValueError("source fill quantity must be positive")
        filled += amount if row["order_side"] == "BUY" else -amount

    if positions.get(capture.instrument_id, _ZERO) != filled:
        raise ValueError("snapshot signed position differs from source fill ledger")
    for order_id, quantity in snapshot.open_orders.items():
        _check_id(order_id, "outstanding client order ID")
        if _decimal(quantity, "remaining open order quantity") <= 0:
            raise ValueError("remaining order quantity must be positive")
        if order_id not in known_orders:
            raise ValueError("snapshot references an order absent from captured lifecycle")
        if last_state[order_id] in _TERMINAL:
            raise ValueError("snapshot reports a terminally canceled/rejected order as open")

    cash = {}
    for currency, value in snapshot.balances.items():
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise ValueError("invalid account cash currency")
        if not isinstance(value, dict) or set(value) != {"total", "available", "locked"}:
            raise ValueError("cash balance requires total/available/locked")
        amounts = {key: _decimal(v, f"{currency} {key}", nonnegative=True) for key, v in value.items()}
        if amounts["total"] != amounts["available"] + amounts["locked"]:
            raise ValueError("cash balance conservation failure")
        cash[currency] = {key: str(v) for key, v in amounts.items()}
    return {
        "snapshot_id": snapshot.snapshot_id,
        "capture_count": snapshot.capture_count,
        "capture_sha256": digest,
        "positions_match": True,
        "signed_quantity": str(filled),
        "cash_arithmetic_ok": True,
        "open_orders_known_ids": len(snapshot.open_orders),
        "order_state_complete": False,
        "source_authenticated": False,
        "equity_reconciled": False,
        "margin_qualified": False,
        "risk_ready": False,
        "cash": cash,
    }


class PaperAccountSnapshotJournal:
    """Append-only snapshots linked to the verified sandbox receipt prefixes.

    Reads verify all prefixes; no automatic repair, foreign balances or
    inferential order/position backfill. Not designed for the order hot path.
    """

    def __init__(self, path: Path, capture: PaperOrderEventCapture, *, account_id: str):
        self.path = Path(path)
        self.capture = capture
        self.account_id = _check_id(account_id, "account ID")
        self._lock = self.path.with_name(self.path.name + ".lock")
        self._head = self.path.with_name(self.path.name + ".head.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._paths()
        with file_lock(self._lock):
            if not self.path.exists():
                if self._head.exists():
                    raise ValueError("snapshot archive missing with existing checkpoint")
                header = _seal(self._header())
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                try:
                    _write(fd, _canonical(header) + b"\n")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _sync_dir(self.path.parent)
                self._head_write(0, header["sha256"], header["sha256"])
            self._read_locked()

    def _header(self):
        return {
            "version": _VERSION,
            "type": "paper-account-snapshot-header",
            "session_id": self.capture.session_id,
            "instrument_id": self.capture.instrument_id,
            "account_id": self.account_id,
            "origin": "sandbox-cache",
        }

    def _paths(self):
        if any(path.is_symlink() for path in (self.path, self._head, self._lock)):
            raise ValueError("paper account snapshot paths must not be symlinks")

    def _head_write(self, sequence: int, last: str, header: str) -> None:
        atomic_json(self._head, _seal({
            "version": _VERSION,
            "sequence": sequence,
            "last_sha256": last,
            "header_sha256": header,
        }))

    def _read_locked(self) -> tuple[list[PaperAccountSnapshot], str, str]:
        self._paths()
        raw = self.path.read_bytes()
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("snapshot archive contains an incomplete record")
        try:
            rows = [parse_json(line.decode("utf-8")) for line in raw.splitlines()]
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ValueError("invalid paper snapshot JSON") from exc
        header = rows[0]
        _check_seal(header, set(self._header()))
        if {k: v for k, v in header.items() if k != "sha256"} != self._header():
            raise ValueError("paper snapshot archive account/session mismatch")
        snapshots: list[PaperAccountSnapshot] = []
        seen_ids: set[str] = set()
        seen_sources: set[str] = set()
        last = header["sha256"]
        for index, row in enumerate(rows[1:], 1):
            _check_seal(row, {"version", "type", "sequence", "previous_sha256", "snapshot"})
            if (
                row["version"] != _VERSION
                or row["type"] != "paper-account-snapshot"
                or type(row["sequence"]) is not int
                or row["sequence"] != index
                or row["previous_sha256"] != last
            ):
                raise ValueError("paper snapshot sequence or predecessor mismatch")
            body = row["snapshot"]
            if not isinstance(body, dict) or set(body) != {f.name for f in fields(PaperAccountSnapshot)}:
                raise ValueError("paper account snapshot field mismatch")
            snapshot = PaperAccountSnapshot(**body)
            validate_snapshot(self.capture, snapshot, self.account_id)
            if (
                snapshot.snapshot_id in seen_ids or snapshot.source_id in seen_sources
                or (snapshots and snapshot.captured_ns <= snapshots[-1].captured_ns)
                or (snapshots and snapshot.capture_count < snapshots[-1].capture_count)
            ):
                raise ValueError("duplicate or out-of-order paper account snapshot")
            seen_ids.add(snapshot.snapshot_id)
            seen_sources.add(snapshot.source_id)
            snapshots.append(snapshot)
            last = row["sha256"]
        if not self._head.exists():
            raise ValueError("snapshot durable checkpoint missing")
        head = read_json(self._head)
        _check_seal(head, {"version", "sequence", "last_sha256", "header_sha256"})
        if (
            head["version"] != _VERSION
            or type(head["sequence"]) is not int
            or head["sequence"] != len(snapshots)
            or head["last_sha256"] != last
            or head["header_sha256"] != header["sha256"]
        ):
            raise ValueError("paper account snapshot/checkpoint mismatch")
        return snapshots, last, header["sha256"]

    def append(self, snapshot: PaperAccountSnapshot) -> dict:
        report = validate_snapshot(self.capture, snapshot, self.account_id)
        with file_lock(self._lock):
            previous, last, header = self._read_locked()
            if (
                any(s.snapshot_id == snapshot.snapshot_id or s.source_id == snapshot.source_id for s in previous)
                or (previous and snapshot.captured_ns <= previous[-1].captured_ns)
                or (previous and snapshot.capture_count < previous[-1].capture_count)
            ):
                raise ValueError("duplicate or out-of-order paper account snapshot")
            # Verify again under the snapshot writer lock; capture appends are
            # independently locked and never rewritten on successful commits.
            validate_snapshot(self.capture, snapshot, self.account_id)
            data = asdict(snapshot)
            record = _seal({
                "version": _VERSION,
                "type": "paper-account-snapshot",
                "sequence": len(previous) + 1,
                "previous_sha256": last,
                "snapshot": data,
            })
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                _write(fd, _canonical(record) + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            self._head_write(len(previous) + 1, record["sha256"], header)
        return report

    def snapshots(self) -> tuple[PaperAccountSnapshot, ...]:
        with file_lock(self._lock):
            rows, _last, _header = self._read_locked()
            return tuple(rows)

    def latest_report(self) -> dict:
        snapshots = self.snapshots()
        if not snapshots:
            raise ValueError("paper account archive has no snapshots")
        return validate_snapshot(self.capture, snapshots[-1], self.account_id)
