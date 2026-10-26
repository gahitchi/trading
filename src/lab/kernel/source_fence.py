"""Offline causal evidence-fence verifier for broker-reported snapshots.

Source stream hashes establish *internal continuity* relative to a prior
anchor supplied by the caller. They are NOT broker signatures, source
authentication, custody evidence, proof of no omitted events before the
anchor, or authority to release an order reservation.

No IO, broker SDK, authentication keys or order-dispatch code is imported.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from lab.kernel.broker_reconciliation import (
    ParityAssessment,
    ReportedSnapshot,
    compare_reported_snapshot,
)
from lab.kernel.order_lifecycle import LifecycleError, OfflineOrderSession, _id, _ns

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_GENESIS = "0" * 64


class SourceChannel(StrEnum):
    ORDERS = "orders"
    EXECUTIONS = "executions"
    POSITIONS = "positions"


_CHANNELS = tuple(SourceChannel)


def _sha(value: str, label: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise LifecycleError(f"invalid {label} SHA-256")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def reported_snapshot_sha256(snapshot: ReportedSnapshot) -> str:
    """Bind exact snapshot data to the reported external fence."""
    if type(snapshot) is not ReportedSnapshot:
        raise LifecycleError("typed broker report required")
    record = {
        "account_scope": snapshot.account_scope,
        "instrument_id": snapshot.instrument_id,
        "action_version": snapshot.action_version,
        "snapshot_id": snapshot.snapshot_id,
        "source_id": snapshot.source_id,
        "source_sha256": snapshot.source_sha256,
        "asof_ns": snapshot.asof_ns,
        "available_ns": snapshot.available_ns,
        "position": str(snapshot.position),
        "orders": [[o.client_order_id, o.broker_order_id, o.status, str(o.cumulative_filled)] for o in snapshot.orders],
        "executions": [
            [e.client_order_id, e.execution_id, str(e.quantity), e.exchange_ns, e.available_ns]
            for e in snapshot.executions
        ],
        "coverage": [
            snapshot.positions_complete,
            snapshot.orders_complete,
            snapshot.executions_complete,
            snapshot.causal_boundary_complete,
        ],
    }
    return hashlib.sha256(_canonical(record).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CursorAnchor:
    channel: SourceChannel
    sequence: int
    digest: str

    def __post_init__(self) -> None:
        if type(self.channel) is not SourceChannel:
            raise LifecycleError("unknown source cursor channel")
        if type(self.sequence) is not int or not 0 <= self.sequence < 2**63:
            raise LifecycleError("invalid source cursor sequence")
        _sha(self.digest, "source cursor")


@dataclass(frozen=True, slots=True)
class SourceFrame:
    channel: SourceChannel
    sequence: int
    event_id: str
    exchange_ns: int
    available_ns: int
    payload_sha256: str
    previous_digest: str
    digest: str

    def __post_init__(self) -> None:
        if type(self.channel) is not SourceChannel:
            raise LifecycleError("unknown frame channel")
        if type(self.sequence) is not int or not 1 <= self.sequence < 2**63:
            raise LifecycleError("invalid frame sequence number")
        _id(self.event_id, "source frame ID")
        _ns(self.exchange_ns, "frame exchange")
        _ns(self.available_ns, "frame availability")
        if self.available_ns < self.exchange_ns:
            raise LifecycleError("frame available before exchange timestamp")
        for name in ("payload_sha256", "previous_digest", "digest"):
            _sha(getattr(self, name), name)


def frame_sha256(
    fence: SourceFence,
    frame: SourceFrame,
) -> str:
    """Hash the scoped content, sequence and predecessor; public, not signed."""
    return hashlib.sha256(
        _canonical(
            [
                fence.account_scope,
                fence.source_id,
                fence.venue,
                fence.environment,
                fence.instrument_id,
                fence.action_version,
                fence.epoch,
                frame.channel.value,
                frame.sequence,
                frame.event_id,
                frame.exchange_ns,
                frame.available_ns,
                frame.payload_sha256,
                frame.previous_digest,
            ]
        ).encode("ascii")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceFence:
    account_scope: str
    source_id: str
    venue: str
    environment: str
    instrument_id: str
    action_version: str
    epoch: str
    snapshot_id: str
    snapshot_sha256: str
    asof_ns: int
    available_ns: int
    anchors: tuple[CursorAnchor, ...]
    frames: tuple[SourceFrame, ...]
    ends: tuple[CursorAnchor, ...]
    source_claims_complete: bool

    def __post_init__(self) -> None:
        for field in (
            "account_scope",
            "source_id",
            "venue",
            "instrument_id",
            "action_version",
            "epoch",
            "snapshot_id",
        ):
            _id(getattr(self, field), field)
        if self.environment not in {"offline_fixture", "paper", "live"}:
            raise LifecycleError("unknown broker source environment")
        _sha(self.snapshot_sha256, "bound snapshot")
        _ns(self.asof_ns, "as-of")
        _ns(self.available_ns, "availability")
        if self.available_ns < self.asof_ns:
            raise LifecycleError("snapshot availability precedes its claimed as-of")
        if type(self.source_claims_complete) is not bool:
            raise LifecycleError("completeness assertion must be explicit bool")
        if type(self.anchors) is not tuple or type(self.frames) is not tuple or type(self.ends) is not tuple:
            raise LifecycleError("source proof collections must be immutable")
        if any(type(x) is not CursorAnchor for x in (*self.anchors, *self.ends)):
            raise LifecycleError("invalid cursor anchor type")
        if any(type(x) is not SourceFrame for x in self.frames):
            raise LifecycleError("invalid source frame type")
        if tuple(x.channel for x in self.anchors) != _CHANNELS:
            raise LifecycleError("all initial channel cursors are required in canonical order")
        if tuple(x.channel for x in self.ends) != _CHANNELS:
            raise LifecycleError("all final channel cursors are required in canonical order")


@dataclass(frozen=True, slots=True)
class FencedParityAssessment:
    account_scope: str
    snapshot_id: str
    findings: tuple[str, ...]

    @property
    def internally_consistent(self) -> bool:
        return not self.findings

    @property
    def source_authenticated(self) -> bool:
        return False

    @property
    def broker_authorization(self) -> bool:
        return False

    @property
    def release_reservations(self) -> bool:
        return False


def assess_fenced_report(
    session: OfflineOrderSession,
    snapshot: ReportedSnapshot,
    fence: SourceFence,
    *,
    expected_anchors: tuple[CursorAnchor, ...],
) -> FencedParityAssessment:
    """Verify a claimed contiguous three-channel source fence and read-only parity.

    expected_anchors are previously persisted *source claims*, not trusted
    broker cursor facts. A clean result can never authorize account actions.
    """
    if type(fence) is not SourceFence or type(expected_anchors) is not tuple:
        raise LifecycleError("typed causal fence and immutable trusted-baseline references required")
    if any(type(x) is not CursorAnchor for x in expected_anchors):
        raise LifecycleError("invalid expected source cursor type")
    base: ParityAssessment = compare_reported_snapshot(session, snapshot)
    errors = set(base.findings)
    if fence.account_scope != session.account_scope or fence.account_scope != snapshot.account_scope:
        errors.add("FENCE_ACCOUNT_SCOPE_MISMATCH")
    if fence.instrument_id != session.instrument_id or fence.instrument_id != snapshot.instrument_id:
        errors.add("FENCE_INSTRUMENT_MISMATCH")
    if fence.action_version != session.action_version or fence.action_version != snapshot.action_version:
        errors.add("FENCE_ACTION_VERSION_MISMATCH")
    if fence.source_id != snapshot.source_id or fence.snapshot_id != snapshot.snapshot_id:
        errors.add("FENCE_SOURCE_OR_SNAPSHOT_ID_MISMATCH")
    if fence.asof_ns != snapshot.asof_ns or fence.available_ns != snapshot.available_ns:
        errors.add("FENCE_BOUNDARY_MISMATCH")
    if fence.snapshot_sha256 != reported_snapshot_sha256(snapshot):
        errors.add("FENCE_SNAPSHOT_DIGEST_MISMATCH")
    if not fence.source_claims_complete:
        errors.add("SOURCE_COMPLETENESS_UNASSERTED")
    if fence.environment != "offline_fixture":
        # The current verifier only operates against internal fixtures.
        # Production paper/live requires an authenticated adapter before this
        # mechanism can even be used as supporting evidence.
        errors.add("EXTERNAL_BROKER_ENVIRONMENT_UNSUPPORTED")
    if fence.anchors != expected_anchors:
        errors.add("FENCE_INITIAL_CURSOR_MISMATCH")

    cursors = {a.channel: a for a in fence.anchors}
    last_available = {channel: 0 for channel in _CHANNELS}
    events_seen: set[tuple[SourceChannel, str]] = set()
    for frame in fence.frames:
        prior = cursors[frame.channel]
        if frame.sequence != prior.sequence + 1:
            errors.add("SOURCE_CURSOR_GAP_OR_REORDER")
        if frame.previous_digest != prior.digest:
            errors.add("SOURCE_CURSOR_HASH_BREAK")
        if frame.digest != frame_sha256(fence, frame):
            errors.add("SOURCE_FRAME_DIGEST_MISMATCH")
        if frame.available_ns < last_available[frame.channel]:
            errors.add("SOURCE_AVAILABILITY_REGRESSION")
        if frame.exchange_ns > fence.asof_ns or frame.available_ns > fence.available_ns:
            errors.add("SOURCE_EVENT_AFTER_SNAPSHOT_BOUNDARY")
        key = (frame.channel, frame.event_id)
        if key in events_seen:
            errors.add("DUPLICATE_SOURCE_EVENT_ID")
        events_seen.add(key)
        cursors[frame.channel] = CursorAnchor(frame.channel, frame.sequence, frame.digest)
        last_available[frame.channel] = frame.available_ns
    for final in fence.ends:
        if cursors[final.channel] != final:
            errors.add("FENCE_END_CURSOR_MISMATCH")
    return FencedParityAssessment(
        account_scope=snapshot.account_scope,
        snapshot_id=snapshot.snapshot_id,
        findings=tuple(sorted(errors)),
    )
