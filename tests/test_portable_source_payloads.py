"""Exact-payload provenance regressions; entirely offline, portable stdlib."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from lab.kernel.broker_reconciliation import ReportedSnapshot
from lab.kernel.order_lifecycle import LifecycleError, OfflineOrderSession
from lab.kernel.source_fence import (
    CursorAnchor,
    ExpectedSourceIdentity,
    SourceChannel,
    SourceFence,
    SourceFrame,
    frame_sha256,
    reported_snapshot_sha256,
)
from lab.kernel.source_payloads import CapturedPayload, assess_payload_bound_report
from lab.markets.journal import EventJournal

ZERO = "0" * 64
HASH = hashlib.sha256(b"test-untrusted-source").hexdigest()


def anchor() -> tuple[CursorAnchor, ...]:
    return tuple(CursorAnchor(x, 0, ZERO) for x in SourceChannel)


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


class SourcePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        journal = EventJournal(Path(temp.name) / "journal.sqlite3")
        self.addCleanup(journal.close)
        self.session = OfflineOrderSession(
            journal,
            account_scope="OFFLINE_ACCOUNT",
            instrument_id="SIM:SPOT",
            action_version="v1",
            starting_position=Decimal("3"),
            quantity_step=Decimal("0.25"),
        )
        self.snapshot = ReportedSnapshot(
            account_scope="OFFLINE_ACCOUNT",
            instrument_id="SIM:SPOT",
            action_version="v1",
            snapshot_id="S1",
            source_id="untrusted-fixture",
            source_sha256=HASH,
            asof_ns=11,
            available_ns=12,
            position=Decimal("3"),
            orders=(),
            executions=(),
            positions_complete=True,
            orders_complete=True,
            executions_complete=True,
            causal_boundary_complete=True,
        )
        self.identity = ExpectedSourceIdentity(
            account_scope="OFFLINE_ACCOUNT",
            source_id="untrusted-fixture",
            venue="FIXTURE",
            environment="offline_fixture",
            instrument_id="SIM:SPOT",
            action_version="v1",
            epoch="genesis",
        )
        self.fence = SourceFence(
            account_scope="OFFLINE_ACCOUNT",
            source_id="untrusted-fixture",
            venue="FIXTURE",
            environment="offline_fixture",
            instrument_id="SIM:SPOT",
            action_version="v1",
            epoch="genesis",
            snapshot_id="S1",
            snapshot_sha256=reported_snapshot_sha256(self.snapshot),
            asof_ns=11,
            available_ns=12,
            anchors=anchor(),
            frames=(),
            ends=anchor(),
            source_claims_complete=True,
        )

    def append(self, channel: SourceChannel, key: str, value: dict, *, at: int = 4):
        proof = self.fence
        current = next(x for x in proof.ends if x.channel is channel)
        payload = CapturedPayload(channel, current.sequence + 1, key, canonical(value))
        frame = SourceFrame(channel, payload.sequence, key, at - 1, at, payload.sha256, current.digest, ZERO)
        frame = replace(frame, digest=frame_sha256(proof, frame))
        ends = tuple(
            CursorAnchor(x.channel, frame.sequence, frame.digest) if x.channel is channel else x
            for x in proof.ends
        )
        self.fence = replace(proof, frames=(*proof.frames, frame), ends=ends)
        return payload

    def assess(self, *payloads: CapturedPayload):
        return assess_payload_bound_report(
            self.session,
            self.snapshot,
            self.fence,
            expected_anchors=anchor(),
            expected_identity=self.identity,
            payloads=payloads,
        )

    def test_matching_full_replay_is_offline_consistent_only(self):
        body = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        before = self.session.state_digest()
        result = self.assess(body)
        self.assertEqual(result.findings, ())
        self.assertTrue(result.internally_consistent)
        self.assertFalse(result.source_authenticated)
        self.assertFalse(result.broker_authorization)
        self.assertFalse(result.release_reservations)
        self.assertEqual(before, self.session.state_digest())

    def test_empty_replay_cannot_pretend_to_contain_position(self):
        self.assertIn("SOURCE_POSITION_PAYLOAD_MISSING", self.assess().findings)

    def test_rehashed_payload_but_wrong_position_is_rejected(self):
        wrong = self.append(SourceChannel.POSITIONS, "P1", {"position": "999"})
        self.assertIn("SOURCE_POSITION_PAYLOAD_DISAGREES", self.assess(wrong).findings)

    def test_missing_matching_frame_bytes_is_rejected(self):
        self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        self.assertIn("SOURCE_PAYLOAD_FRAME_COVERAGE_MISMATCH", self.assess().findings)

    def test_tampered_payload_without_corresponding_digest_is_rejected(self):
        payload = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        replaced = replace(payload, canonical_json=canonical({"position": "4"}))
        self.assertIn("SOURCE_PAYLOAD_DIGEST_MISMATCH", self.assess(replaced).findings)

    def test_foreign_frame_same_body_is_not_allowed(self):
        payload = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        other = replace(payload, event_id="different-event")
        self.assertIn("SOURCE_PAYLOAD_FRAME_COVERAGE_MISMATCH", self.assess(other).findings)

    def test_non_genesis_history_never_qualifies(self):
        payload = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        anchored = tuple(
            CursorAnchor(x.channel, 1, HASH) if x.channel is SourceChannel.ORDERS else x for x in anchor()
        )
        self.fence = replace(self.fence, anchors=anchored)
        self.assertIn("SOURCE_PAYLOAD_REPLAY_NOT_FROM_GENESIS", self.assess(payload).findings)

    def test_malformed_duplicate_noncanonical_and_nonfinite_payloads_fail(self):
        invalid = (
            b'{"position":"3","position":"4"}',
            b'{"position": "3"}',
            b'{"position":1e309}',
            b'{"position":NaN}',
            b'{"position":3}',
            b'{"position":"Infinity"}',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(LifecycleError):
                CapturedPayload(SourceChannel.POSITIONS, 1, "P1", raw)

    def test_reject_boolean_execution_timestamps_and_unsupported_event_types(self):
        with self.assertRaises(LifecycleError):
            CapturedPayload(
                SourceChannel.EXECUTIONS,
                1,
                "EX",
                canonical({
                    "client_order_id": "A", "execution_id": "EX", "quantity": "1",
                    "exchange_ns": True, "available_ns": 3,
                }),
            )
        with self.assertRaises(LifecycleError):
            CapturedPayload(SourceChannel.ORDERS, 1, "O1", canonical({"position": "3"}))

    def test_cannot_override_result_with_broker_authority(self):
        report = self.assess()
        with self.assertRaises(TypeError):
            type(report)(account_scope="OFFLINE_ACCOUNT", snapshot_id="S1", findings=(), broker_authorization=True)
        with self.assertRaises((AttributeError, TypeError)):
            report.release_reservations = True

    def test_extra_execution_cannot_be_hidden_by_complete_snapshot(self):
        pos = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        execution = self.append(SourceChannel.EXECUTIONS, "EX1", {
            "client_order_id": "A", "execution_id": "EX1", "quantity": "1",
            "exchange_ns": 4, "available_ns": 5,
        }, at=5)
        self.assertIn("SOURCE_EXECUTION_PAYLOAD_DISAGREES", self.assess(pos, execution).findings)

    def test_source_time_forgery_is_detected_even_with_valid_frame_hash(self):
        pos = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        execution = self.append(SourceChannel.EXECUTIONS, "EX1", {
            "client_order_id": "A", "execution_id": "EX1", "quantity": "1",
            "exchange_ns": 1, "available_ns": 5,
        }, at=5)
        self.assertIn("SOURCE_EXECUTION_TIME_MISMATCH", self.assess(pos, execution).findings)

    def test_duplicate_source_payloads_cannot_qualify(self):
        pos = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        self.assertIn("DUPLICATE_SOURCE_PAYLOAD", self.assess(pos, pos).findings)

    def test_external_environment_remains_refused(self):
        pos = self.append(SourceChannel.POSITIONS, "P1", {"position": "3"})
        self.fence = replace(self.fence, environment="paper")
        result = self.assess(pos)
        self.assertIn("EXTERNAL_BROKER_ENVIRONMENT_UNSUPPORTED", result.findings)
        self.assertFalse(result.source_authenticated)


if __name__ == "__main__":
    unittest.main()
