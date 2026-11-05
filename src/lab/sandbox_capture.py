"""Sandbox order-event capture: durable receipt stream, no exchange access.

The event source is NautilusTrader's simulated execution client. This
archive preserves selected event fields but does not authenticate them,
guarantee a complete account history or authorize profit accounting.
A bad receipt, changed file or checkpoint failure blocks future appends.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lab.persistence import atomic_json, file_lock, parse_json, read_json

_VERSION = 1
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_KIND = {
    "OrderInitialized", "OrderSubmitted", "OrderAccepted", "OrderRejected",
    "OrderDenied", "OrderCanceled", "OrderExpired", "OrderPendingCancel",
    "OrderCancelRejected", "OrderUpdated", "OrderPendingUpdate",
    "OrderModifyRejected", "OrderFilled", "OrderFillVoided",
}
_FILL_FIELDS = (
    "trade_id", "venue_order_id", "account_id", "order_side", "order_type",
    "last_qty", "last_px", "currency", "liquidity_side", "commission",
    "position_id", "reconciliation",
)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _seal(value: dict) -> dict:
    return {**value, "sha256": hashlib.sha256(_canonical(value)).hexdigest()}


def _verify(value: dict, fields: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != fields | {"sha256"}:
        raise ValueError("sandbox capture record schema mismatch")
    digest = value["sha256"]
    if not isinstance(digest, str) or digest != _seal({k: v for k, v in value.items() if k != "sha256"})["sha256"]:
        raise ValueError("sandbox capture hash mismatch")


def _positive_ns(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer nanosecond timestamp")
    return value


def _required_string(value, name: str) -> str:
    if value is None:
        raise ValueError(f"sandbox event missing {name}")
    result = str(value)
    if not result or result == "None" or len(result) > 2048:
        raise ValueError(f"sandbox event has invalid {name}")
    return result


def event_receipt(event, *, received_ns: int, instrument_id: str) -> dict:
    """Extract known NautilusTrader event fields without guessed economics.

    Commission currency is preserved in the original Money representation;
    a missing commission remains NULL, never a manufactured zero.
    Fill restatements/voids are captured but not treated as new trades.
    """
    kind = type(event).__name__
    if kind not in _KIND:
        raise ValueError(f"unsupported NautilusTrader order event: {kind}")
    received_ns = _positive_ns(received_ns, "capture time")
    common = {
        key: _required_string(getattr(event, key, None), key)
        for key in ("event_id", "trader_id", "strategy_id", "instrument_id", "client_order_id")
    }
    if common["instrument_id"] != instrument_id:
        raise ValueError("order event from another instrument or account scope")
    common["kind"] = kind
    common["ts_event_ns"] = _positive_ns(getattr(event, "ts_event", None), "event time")
    common["ts_init_ns"] = _positive_ns(getattr(event, "ts_init", None), "initialization time")
    common["received_ns"] = received_ns
    common["causation_id"] = None if getattr(event, "causation_id", None) is None else str(event.causation_id)
    if kind == "OrderFilled":
        for field in _FILL_FIELDS:
            value = getattr(event, field, None)
            if field in {"commission", "position_id"}:
                common[field] = None if value is None else str(value)
            elif field == "reconciliation":
                if type(value) is not bool:
                    raise ValueError("sandbox fill reconciliation flag must be boolean")
                common[field] = value
            else:
                common[field] = _required_string(value, field)
        for field in ("last_qty", "last_px"):
            try:
                amount = Decimal(common[field])
            except InvalidOperation as exc:
                raise ValueError(f"invalid sandbox fill {field}") from exc
            if not amount.is_finite() or amount <= 0:
                raise ValueError(f"invalid sandbox fill {field}")
        if common["order_side"] not in {"BUY", "SELL"}:
            raise ValueError("invalid sandbox fill side")
    return common


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(fd: int, data: bytes) -> None:
    rest = memoryview(data)
    while rest:
        size = os.write(fd, rest)
        if size <= 0:
            raise OSError("sandbox capture append made no progress")
        rest = rest[size:]


class PaperOrderEventCapture:
    """Exclusive append-only sandbox order receipt log (NOT an economic ledger)."""

    def __init__(self, path: Path, *, session_id: str, instrument_id: str):
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise ValueError("invalid sandbox session ID")
        self.path = Path(path)
        self.session_id = session_id
        self.instrument_id = _required_string(instrument_id, "instrument")
        self._lock = self.path.with_name(self.path.name + ".lock")
        self._head = self.path.with_name(self.path.name + ".head.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_paths()
        with file_lock(self._lock):
            if not self.path.exists():
                if self._head.exists():
                    raise ValueError("capture log missing but checkpoint exists")
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

    def _header(self) -> dict:
        return {
            "type": "sandbox-order-header",
            "version": _VERSION,
            "session_id": self.session_id,
            "instrument_id": self.instrument_id,
            "origin": "nautilus-sandbox",
        }

    def _check_paths(self) -> None:
        if any(p.is_symlink() for p in (self.path, self._lock, self._head)):
            raise ValueError("capture paths must not be symlinks")

    def _head_write(self, count: int, last: str, header: str) -> None:
        atomic_json(self._head, _seal({
            "version": _VERSION, "count": count, "last_sha256": last, "header_sha256": header
        }))

    def _read_locked(self) -> tuple[list[dict], str, str]:
        self._check_paths()
        raw = self.path.read_bytes()
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("incomplete sandbox capture line")
        try:
            rows = [parse_json(line.decode("utf-8")) for line in raw.splitlines()]
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ValueError("invalid sandbox capture JSON") from exc
        header = rows[0]
        _verify(header, set(self._header()))
        if {k: v for k, v in header.items() if k != "sha256"} != self._header():
            raise ValueError("sandbox capture source/session mismatch")
        seen: set[str] = set()
        last = header["sha256"]
        receipts = []
        for index, row in enumerate(rows[1:], 1):
            _verify(row, {"type", "version", "sequence", "previous_sha256", "receipt"})
            if (
                row["type"] != "sandbox-order-event" or row["version"] != _VERSION
                or type(row["sequence"]) is not int or row["sequence"] != index
                or row["previous_sha256"] != last
            ):
                raise ValueError("sandbox capture order/sequence mismatch")
            receipt = row["receipt"]
            if not isinstance(receipt, dict) or not isinstance(receipt.get("event_id"), str):
                raise ValueError("invalid sandbox source receipt")
            if receipt["event_id"] in seen:
                raise ValueError("duplicate sandbox event ID")
            if receipt.get("instrument_id") != self.instrument_id or receipt.get("kind") not in _KIND:
                raise ValueError("capture contains unexpected instrument/event")
            seen.add(receipt["event_id"])
            receipts.append(receipt)
            last = row["sha256"]
        if not self._head.exists():
            raise ValueError("sandbox capture checkpoint missing")
        checkpoint = read_json(self._head)
        _verify(checkpoint, {"version", "count", "last_sha256", "header_sha256"})
        if (
            checkpoint["version"] != _VERSION
            or type(checkpoint["count"]) is not int
            or checkpoint["count"] != len(receipts)
            or checkpoint["last_sha256"] != last
            or checkpoint["header_sha256"] != header["sha256"]
        ):
            raise ValueError("sandbox capture/checkpoint mismatch")
        return receipts, last, header["sha256"]

    def append(self, event, *, received_ns: int) -> None:
        receipt = event_receipt(event, received_ns=received_ns, instrument_id=self.instrument_id)
        with file_lock(self._lock):
            receipts, last, header = self._read_locked()
            if any(row["event_id"] == receipt["event_id"] for row in receipts):
                raise ValueError("duplicate sandbox event ID")
            record = _seal({
                "type": "sandbox-order-event",
                "version": _VERSION,
                "sequence": len(receipts) + 1,
                "previous_sha256": last,
                "receipt": receipt,
            })
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                _write(fd, _canonical(record) + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            self._head_write(len(receipts) + 1, record["sha256"], header)

    def receipts(self) -> tuple[dict, ...]:
        with file_lock(self._lock):
            rows, _last, _header = self._read_locked()
            return tuple(rows)

    def compare_flat_start_positions(self, position_quantities: dict[str, str]) -> dict[str, str]:
        """Fail on missing fills/ambiguous corrections or position disagreement.

        This comparison is valid ONLY if independently established flat
        inventory at session start and no other account/order source.
        It never invents marks, fees, funding or profit allocations.
        """
        receipts = self.receipts()
        filled: dict[str, Decimal] = {}
        seen_trades: set[tuple[str, str]] = set()
        for receipt in receipts:
            if receipt["kind"] == "OrderFillVoided":
                raise ValueError("voided fills require independent correction reconciliation")
            if receipt["kind"] != "OrderFilled":
                continue
            key = (receipt["account_id"], receipt["trade_id"])
            if key in seen_trades:
                raise ValueError("duplicate or restated trade requires independent reconciliation")
            seen_trades.add(key)
            symbol = receipt["instrument_id"]
            quantity = Decimal(receipt["last_qty"])
            filled[symbol] = filled.get(symbol, Decimal(0)) + (
                quantity if receipt["order_side"] == "BUY" else -quantity
            )
        if not isinstance(position_quantities, dict) or any(
            not isinstance(symbol, str) or not isinstance(qty, str)
            for symbol, qty in position_quantities.items()
        ):
            raise ValueError("independent position snapshot must use exact decimal strings")
        try:
            observed = {symbol: Decimal(qty) for symbol, qty in position_quantities.items()}
        except InvalidOperation as exc:
            raise ValueError("invalid independent position quantity") from exc
        if any(not qty.is_finite() for qty in observed.values()):
            raise ValueError("nonfinite independent position")
        for symbol in set(filled) | set(observed):
            if filled.get(symbol, Decimal(0)) != observed.get(symbol, Decimal(0)):
                raise ValueError(f"paper fill/position mismatch for {symbol}")
        return {symbol: str(qty) for symbol, qty in sorted(filled.items())}
