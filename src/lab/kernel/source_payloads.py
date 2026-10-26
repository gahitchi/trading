"""Audit-only binding of claimed source-frame digests to decoded offline payloads.

A consistent hash and decoded snapshot are not broker authentication. This
module accepts no credentials, network connection, or order submission route.
The full-replay limitation is intentional: without trusted predecessor state
an incremental stream cannot prove the contents of an existing snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from lab.kernel.broker_reconciliation import ReportedExecution, ReportedOrder, ReportedSnapshot
from lab.kernel.order_lifecycle import LifecycleError, OfflineOrderSession, _id
from lab.kernel.source_fence import (
    CursorAnchor,
    ExpectedSourceIdentity,
    FencedParityAssessment,
    SourceChannel,
    SourceFence,
    assess_fenced_report,
)

_GENESIS = "0" * 64
_MAX_PAYLOAD = 8192
_FIELDS = {
    SourceChannel.ORDERS: frozenset({"client_order_id", "broker_order_id", "status", "cumulative_filled"}),
    SourceChannel.EXECUTIONS: frozenset({"client_order_id", "execution_id", "quantity", "exchange_ns", "available_ns"}),
    SourceChannel.POSITIONS: frozenset({"position"}),
}


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleError("duplicate JSON payload key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise LifecycleError("non-finite JSON number in source payload")


def _decode(payload: bytes, channel: SourceChannel) -> dict[str, object]:
    if type(payload) is not bytes or not 0 < len(payload) <= _MAX_PAYLOAD:
        raise LifecycleError("source payload must be bounded bytes")
    try:
        raw = payload.decode("ascii")
        decoded = json.loads(
            raw, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant, parse_float=_invalid_constant
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("invalid canonical source payload") from exc
    if type(decoded) is not dict or frozenset(decoded) != _FIELDS[channel]:
        raise LifecycleError("source payload schema mismatch")
    encoded = json.dumps(decoded, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if raw != encoded:
        raise LifecycleError("source payload is not canonical JSON")
    return decoded


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    if type(value) is not str or not 0 < len(value) <= 64:
        raise LifecycleError("payload quantity must be an exact Decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise LifecycleError("invalid source Decimal") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise LifecycleError("invalid source Decimal value")
    return number


def _record(channel: SourceChannel, raw: dict[str, object]) -> object:
    if channel is SourceChannel.ORDERS:
        return ReportedOrder(
            client_order_id=raw["client_order_id"],
            broker_order_id=raw["broker_order_id"],
            status=raw["status"],
            cumulative_filled=_decimal(raw["cumulative_filled"]),
        )
    if channel is SourceChannel.EXECUTIONS:
        # Guard bool (a Python int subclass) before timestamp validation.
        if type(raw["exchange_ns"]) is not int or type(raw["available_ns"]) is not int:
            raise LifecycleError("execution timestamps must be exact integer nanoseconds")
        return ReportedExecution(
            client_order_id=raw["client_order_id"],
            execution_id=raw["execution_id"],
            quantity=_decimal(raw["quantity"], positive=True),
            exchange_ns=raw["exchange_ns"],
            available_ns=raw["available_ns"],
        )
    return _decimal(raw["position"])


@dataclass(frozen=True, slots=True)
class CapturedPayload:
    """An exact, canonical event body observed by an *untrusted* source."""

    channel: SourceChannel
    sequence: int
    event_id: str
    canonical_json: bytes

    def __post_init__(self) -> None:
        if type(self.channel) is not SourceChannel:
            raise LifecycleError("invalid captured payload channel")
        if type(self.sequence) is not int or not 1 <= self.sequence < 2**63:
            raise LifecycleError("invalid captured sequence")
        _id(self.event_id, "captured event ID")
        _record(self.channel, _decode(self.canonical_json, self.channel))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json).hexdigest()


def assess_payload_bound_report(
    session: OfflineOrderSession,
    snapshot: ReportedSnapshot,
    fence: SourceFence,
    *,
    expected_anchors: tuple[CursorAnchor, ...],
    expected_identity: ExpectedSourceIdentity,
    payloads: tuple[CapturedPayload, ...],
) -> FencedParityAssessment:
    """Require exact source payload coverage before claiming *offline* parity.

    No result permits order dispatch, custody attribution, or reservation
    release. Replayed events are required from zero-valued genesis anchors;
    incremental broker history is explicitly unqualified until an actual
    provider-authenticated predecessor checkpoint is implemented.
    """
    if type(payloads) is not tuple or any(type(p) is not CapturedPayload for p in payloads):
        raise LifecycleError("immutable typed source payload tuple required")
    base = assess_fenced_report(
        session, snapshot, fence, expected_anchors=expected_anchors, expected_identity=expected_identity
    )
    findings = set(base.findings)
    if any(anchor.sequence != 0 or anchor.digest != _GENESIS for anchor in fence.anchors):
        findings.add("SOURCE_PAYLOAD_REPLAY_NOT_FROM_GENESIS")

    actual: dict[tuple[SourceChannel, int, str], CapturedPayload] = {}
    for payload in payloads:
        key = (payload.channel, payload.sequence, payload.event_id)
        if key in actual:
            findings.add("DUPLICATE_SOURCE_PAYLOAD")
        actual[key] = payload
    announced = {(f.channel, f.sequence, f.event_id) for f in fence.frames}
    if actual.keys() != announced:
        findings.add("SOURCE_PAYLOAD_FRAME_COVERAGE_MISMATCH")

    orders: dict[str, ReportedOrder] = {}
    executions: dict[str, ReportedExecution] = {}
    position: Decimal | None = None
    for frame in fence.frames:
        payload = actual.get((frame.channel, frame.sequence, frame.event_id))
        if payload is None:
            continue
        if payload.sha256 != frame.payload_sha256:
            findings.add("SOURCE_PAYLOAD_DIGEST_MISMATCH")
            # A corrupted or substituted payload must not contribute to parity.
            continue
        record = _record(payload.channel, _decode(payload.canonical_json, payload.channel))
        if frame.channel is SourceChannel.ORDERS:
            orders[record.client_order_id] = record
        elif frame.channel is SourceChannel.EXECUTIONS:
            if (record.exchange_ns, record.available_ns) != (frame.exchange_ns, frame.available_ns):
                findings.add("SOURCE_EXECUTION_TIME_MISMATCH")
            if record.execution_id in executions:
                findings.add("DUPLICATE_SOURCE_EXECUTION")
            executions[record.execution_id] = record
        else:
            position = record
    if position is None:
        findings.add("SOURCE_POSITION_PAYLOAD_MISSING")
    elif position != snapshot.position:
        findings.add("SOURCE_POSITION_PAYLOAD_DISAGREES")
    if orders != {item.client_order_id: item for item in snapshot.orders}:
        findings.add("SOURCE_ORDER_PAYLOAD_DISAGREES")
    if executions != {item.execution_id: item for item in snapshot.executions}:
        findings.add("SOURCE_EXECUTION_PAYLOAD_DISAGREES")
    return FencedParityAssessment(
        account_scope=snapshot.account_scope,
        snapshot_id=snapshot.snapshot_id,
        findings=tuple(sorted(findings)),
    )
