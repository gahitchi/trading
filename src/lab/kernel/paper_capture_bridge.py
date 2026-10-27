"""Strict read-only conversion of private Nautilus/IBKR paper captures.

This is not a broker client or an authenticated source. Even byte-for-byte
matching reports keep ALL coverage flags false: DataActor cache reads are not
atomic broker-history queries, and no broker-signed cursor is available.
Never send its output to an order dispatcher or use it to release reservations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from lab.kernel.broker_reconciliation import (
    ParityAssessment,
    ReportedExecution,
    ReportedOrder,
    ReportedSnapshot,
    compare_reported_snapshot,
)
from lab.kernel.order_lifecycle import LifecycleError, OfflineOrderSession, _id, _ns, _quantity
from lab.safety import check_paper_settings

_REQUIRED = (
    "identity.json",
    "broker_account.json",
    "broker_positions.json",
    "broker_orders.json",
    "broker_capture_meta.json",
)
_MAX_FILE_BYTES = 8 * 1024 * 1024
_STATUS = {
    "ACCEPTED": "open",
    "SUBMITTED": "open",
    "INITIALIZED": "unknown",
    "PENDING_NEW": "unknown",
    "PARTIALLY_FILLED": "open",
    "PENDING_CANCEL": "pending_cancel",
    "CANCEL_PENDING": "pending_cancel",
    "CANCELED": "cancelled",
    "CANCELLED": "cancelled",
    "FILLED": "filled",
    "REJECTED": "rejected",
}


@dataclass(frozen=True, slots=True)
class PaperCaptureObservation:
    """A non-authoritative projection of private read-only source files."""

    snapshot: ReportedSnapshot
    manifest_sha256: str

    @property
    def broker_authenticated(self) -> bool:
        return False

    @property
    def can_submit_orders(self) -> bool:
        return False

    @property
    def can_release_reservations(self) -> bool:
        return False

    def compare(self, session: OfflineOrderSession) -> ParityAssessment:
        """Read-only parity: coverage gaps always remain blocking findings."""
        return compare_reported_snapshot(session, self.snapshot)


def _read_private_capture(folder: Path) -> tuple[dict[str, object], str]:
    """Read a bounded, non-symlinked immutable-looking copy (not an integrity root)."""
    if folder.is_symlink() or not folder.is_dir():
        raise LifecycleError("capture must be a private real directory")
    documents: dict[str, object] = {}
    digests: list[list[str]] = []
    for name in _REQUIRED:
        target = folder / name
        if target.is_symlink() or not target.is_file():
            raise LifecycleError("capture is missing a required regular file")
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise LifecycleError("capture file exceeds the size limit")
        raw = target.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            raise LifecycleError("capture file exceeds the size limit")
        try:
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise LifecycleError("invalid capture JSON") from exc
        if type(data) not in (dict, list):
            raise LifecycleError("capture document has invalid shape")
        documents[name] = data
        digests.append([name, hashlib.sha256(raw).hexdigest()])
    manifest = hashlib.sha256(json.dumps(digests, separators=(",", ":")).encode("ascii")).hexdigest()
    return documents, manifest


def _decimal_string(value: object, label: str, *, positive: bool = False) -> Decimal:
    if type(value) is not str or len(value) > 100:
        raise LifecycleError(f"{label} must be an exact decimal string")
    try:
        amount = Decimal(value)
    except ArithmeticError as exc:
        raise LifecycleError(f"{label} is not a decimal") from exc
    return _quantity(amount, label, positive=positive)


def _positive_ns(value: object, label: str) -> int:
    _ns(value, label)
    if value == 0:
        raise LifecycleError(f"{label} cannot be zero")
    return value


def _position(row: object) -> Decimal:
    if type(row) is not dict or type(row.get("positions")) is not list:
        raise LifecycleError("exact broker position components are required")
    total = Decimal("0")
    for part in row["positions"]:
        if type(part) is not dict:
            raise LifecycleError("invalid position lot")
        side = str(part.get("side", "")).upper().split(".")[-1]
        if side not in {"LONG", "SHORT"}:
            raise LifecycleError("unknown position side")
        amount = _decimal_string(part.get("quantity"), "position quantity", positive=True)
        total += amount if side == "LONG" else -amount
    exact = _decimal_string(row.get("net_exact"), "exact position")
    if total != exact:
        raise LifecycleError("broker position components disagree with exact net")
    return exact


def load_paper_capture(
    folder: str | Path,
    *,
    expected_account: str,
    ticker: str,
    instrument_id: str,
    action_version: str,
) -> PaperCaptureObservation:
    """Convert a private raw capture without assuming completeness or authentication.

    `asof_ns` means *local capture completion*, NOT a synchronized broker cut.
    Missing venue-order identities, missing full fill histories, or pre-v2 float
    positions fail closed rather than fabricating matched broker evidence.
    """
    _id(instrument_id, "instrument")
    _id(action_version, "action version")
    _id(ticker, "ticker")
    if type(expected_account) is not str or not expected_account.startswith("IB-"):
        raise LifecycleError("expected IB paper account must include the broker prefix")
    check_paper_settings(4002, expected_account.removeprefix("IB-"))
    documents, manifest = _read_private_capture(Path(folder))
    identity = documents["identity.json"]
    account = documents["broker_account.json"]
    positions = documents["broker_positions.json"]
    orders = documents["broker_orders.json"]
    meta = documents["broker_capture_meta.json"]
    if any(type(part) is not dict for part in (identity, account, positions, meta)) or type(orders) is not list:
        raise LifecycleError("invalid capture collection types")
    if account.get("account_id") != expected_account:
        raise LifecycleError("reported broker account differs from expected paper account")
    check_paper_settings(identity.get("paper_port"), expected_account.removeprefix("IB-"))
    if meta.get("stream") != "NAUTILUS_IBKR_READ_ONLY":
        raise LifecycleError("capture stream is not the recognized read-only adapter")
    if any(
        meta.get(flag) is not False
        for flag in (
            "coverage_verified",
            "broker_authentication_verified",
            "causal_boundary_verified",
        )
    ):
        raise LifecycleError("read-only capture cannot claim authenticated or complete broker history")
    begun = _positive_ns(meta.get("capture_started_ns"), "capture start")
    finished = _positive_ns(meta.get("capture_completed_ns"), "capture completion")
    if finished < begun:
        raise LifecycleError("capture interval is reversed")
    if ticker not in positions:
        raise LifecycleError("requested instrument has no broker position record")
    position = _position(positions[ticker])
    converted_orders: list[ReportedOrder] = []
    converted_fills: list[ReportedExecution] = []
    for entry in orders:
        if type(entry) is not dict:
            raise LifecycleError("invalid broker order row")
        if entry.get("instrument_id") != instrument_id:
            continue
        client_id = _id(entry.get("client_order_id"), "client order")
        venue_id = _id(entry.get("venue_order_id"), "venue order")
        status = entry.get("status")
        if type(status) is not str:
            raise LifecycleError("missing broker order status")
        state = _STATUS.get(status.upper().split(".")[-1], "unknown")
        filled = _decimal_string(entry.get("filled_qty"), "cumulative fill")
        if filled < 0:
            raise LifecycleError("negative broker cumulative fill")
        original_quantity = _decimal_string(entry.get("quantity"), "original order quantity", positive=True)
        if filled > original_quantity:
            raise LifecycleError("reported order was overfilled")
        events = entry.get("events")
        if type(events) is not list:
            raise LifecycleError("broker order event history missing")
        for event in events:
            if type(event) is not dict:
                raise LifecycleError("invalid broker event")
            if event.get("type") != "OrderFilled":
                continue
            execution_id = _id(event.get("trade_id"), "broker execution")
            quantity = _decimal_string(event.get("last_qty"), "execution quantity", positive=True)
            at = _positive_ns(event.get("ts_event"), "execution")
            if at > finished:
                raise LifecycleError("execution timestamp lies beyond local capture")
            converted_fills.append(ReportedExecution(client_id, execution_id, quantity, at, finished))
        if sum((e.quantity for e in converted_fills if e.client_order_id == client_id), Decimal("0")) != filled:
            raise LifecycleError("capture lacks exact complete per-order execution history")
        converted_orders.append(ReportedOrder(client_id, venue_id, state, filled))
    snapshot = ReportedSnapshot(
        account_scope=expected_account,
        instrument_id=instrument_id,
        action_version=action_version,
        snapshot_id="CAP-" + manifest[:32],
        source_id="NAUTILUS_IBKR_CAPTURE_UNVERIFIED",
        source_sha256=manifest,
        asof_ns=finished,
        available_ns=finished,
        position=position,
        orders=tuple(converted_orders),
        executions=tuple(converted_fills),
        positions_complete=False,
        orders_complete=False,
        executions_complete=False,
        causal_boundary_complete=False,
    )
    return PaperCaptureObservation(snapshot=snapshot, manifest_sha256=manifest)
