"""Adversarial offline causal fence tests, independently runnable on Termux."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from lab.kernel.broker_reconciliation import ReportedSnapshot
from lab.kernel.order_lifecycle import LifecycleError, OfflineOrderSession
from lab.kernel.source_fence import (
    CursorAnchor,
    SourceChannel,
    SourceFence,
    SourceFrame,
    assess_fenced_report,
    frame_sha256,
    reported_snapshot_sha256,
)
from lab.markets.journal import EventJournal

ROOT = Path(__file__).resolve().parents[1]
HASH = hashlib.sha256(b"synthetic-cursor-source").hexdigest()
GENESIS = "0" * 64


def anchors() -> tuple[CursorAnchor, ...]:
    return tuple(CursorAnchor(channel, 0, GENESIS) for channel in SourceChannel)


class PortableSourceFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = EventJournal(Path(self.tmp.name) / "source-fence.sqlite3")
        self.addCleanup(self.journal.close)
        self.session = OfflineOrderSession(
            self.journal,
            account_scope="SIM_ACCOUNT",
            instrument_id="SIM:FENCE",
            action_version="version-1",
            starting_position=Decimal("3"),
            quantity_step=Decimal("0.25"),
        )
        self.snapshot = ReportedSnapshot(
            account_scope="SIM_ACCOUNT",
            instrument_id="SIM:FENCE",
            action_version="version-1",
            snapshot_id="snap-1",
            source_id="offline-source",
            source_sha256=HASH,
            asof_ns=10,
            available_ns=12,
            position=Decimal("3"),
            orders=(),
            executions=(),
            positions_complete=True,
            orders_complete=True,
            executions_complete=True,
            causal_boundary_complete=True,
        )
        self.fence = SourceFence(
            account_scope="SIM_ACCOUNT",
            source_id="offline-source",
            venue="FIXTURE",
            environment="offline_fixture",
            instrument_id="SIM:FENCE",
            action_version="version-1",
            epoch="boot-1",
            snapshot_id="snap-1",
            snapshot_sha256=reported_snapshot_sha256(self.snapshot),
            asof_ns=10,
            available_ns=12,
            anchors=anchors(),
            frames=(),
            ends=anchors(),
            source_claims_complete=True,
        )

    def evaluate(self, fence=None, snapshot=None, expected=None):
        return assess_fenced_report(
            self.session,
            self.snapshot if snapshot is None else snapshot,
            self.fence if fence is None else fence,
            expected_anchors=anchors() if expected is None else expected,
        )

    def appended(self, proof: SourceFence, channel: SourceChannel, event: str, *, seq=None, available=4):
        current = {item.channel: item for item in proof.ends}[channel]
        new_frame = SourceFrame(
            channel=channel,
            sequence=current.sequence + 1 if seq is None else seq,
            event_id=event,
            exchange_ns=available - 1,
            available_ns=available,
            payload_sha256=HASH,
            previous_digest=current.digest,
            digest=GENESIS,
        )
        new_frame = replace(new_frame, digest=frame_sha256(proof, new_frame))
        ends = tuple(
            CursorAnchor(item.channel, new_frame.sequence, new_frame.digest) if item.channel is channel else item
            for item in proof.ends
        )
        return replace(proof, frames=(*proof.frames, new_frame), ends=ends)

    def test_all_three_empty_channels_are_consistent_but_never_authenticated(self):
        result = self.evaluate()
        self.assertTrue(result.internally_consistent)
        self.assertEqual(result.findings, ())
        self.assertFalse(result.source_authenticated)
        self.assertFalse(result.broker_authorization)
        self.assertFalse(result.release_reservations)

    def test_three_interleaved_contiguous_channels_pass_without_authority(self):
        fence = self.appended(self.fence, SourceChannel.ORDERS, "order-1")
        fence = self.appended(fence, SourceChannel.POSITIONS, "position-1", available=5)
        fence = self.appended(fence, SourceChannel.EXECUTIONS, "exec-1", available=6)
        fence = self.appended(fence, SourceChannel.ORDERS, "order-2", available=7)
        result = self.evaluate(fence)
        self.assertEqual(result.findings, ())
        self.assertFalse(result.broker_authorization)

    def test_missing_sequence_number_fails_even_when_chain_hash_is_valid(self):
        frame = self.appended(self.fence, SourceChannel.ORDERS, "order-1")
        forged = replace(frame.frames[0], sequence=3)
        forged = replace(forged, digest=frame_sha256(frame, forged))
        fence = replace(
            frame, frames=(forged,), ends=(CursorAnchor(SourceChannel.ORDERS, 3, forged.digest), *frame.ends[1:])
        )
        self.assertIn("SOURCE_CURSOR_GAP_OR_REORDER", self.evaluate(fence).findings)

    def test_breaking_previous_digest_fails_even_with_recomputed_frame_hash(self):
        first = self.appended(self.fence, SourceChannel.ORDERS, "order-1")
        broken = replace(first.frames[0], previous_digest=HASH)
        broken = replace(broken, digest=frame_sha256(first, broken))
        fence = replace(
            first, frames=(broken,), ends=(CursorAnchor(SourceChannel.ORDERS, 1, broken.digest), *first.ends[1:])
        )
        self.assertIn("SOURCE_CURSOR_HASH_BREAK", self.evaluate(fence).findings)

    def test_altered_payload_without_recomputing_digest_fails(self):
        frame = self.appended(self.fence, SourceChannel.EXECUTIONS, "exec-1")
        altered = replace(frame.frames[0], payload_sha256=hashlib.sha256(b"another payload").hexdigest())
        fence = replace(frame, frames=(altered,))
        self.assertIn("SOURCE_FRAME_DIGEST_MISMATCH", self.evaluate(fence).findings)

    def test_truncated_stream_fails_final_cursor_match(self):
        first = self.appended(self.fence, SourceChannel.EXECUTIONS, "exec-1")
        second = self.appended(first, SourceChannel.EXECUTIONS, "exec-2")
        truncated = replace(second, frames=first.frames)
        self.assertIn("FENCE_END_CURSOR_MISMATCH", self.evaluate(truncated).findings)

    def test_caller_supplied_new_cursor_anchor_does_not_match_prior_epoch(self):
        changed = replace(
            self.fence,
            anchors=tuple(
                CursorAnchor(item.channel, 1, HASH) if item.channel is SourceChannel.ORDERS else item
                for item in self.fence.anchors
            ),
        )
        self.assertIn("FENCE_INITIAL_CURSOR_MISMATCH", self.evaluate(changed).findings)

    def test_swap_snapshot_content_is_detected_by_bound_fingerprint(self):
        altered = replace(self.snapshot, position=Decimal("4"))
        result = self.evaluate(snapshot=altered)
        self.assertIn("FENCE_SNAPSHOT_DIGEST_MISMATCH", result.findings)
        self.assertIn("POSITION_DISAGREES_WITH_LOCAL_FILL_PROJECTION", result.findings)

    def test_cross_account_version_source_or_snapshot_never_reused(self):
        for key, value, expected in (
            ("account_scope", "OTHER_ACCOUNT", "FENCE_ACCOUNT_SCOPE_MISMATCH"),
            ("action_version", "version-2", "FENCE_ACTION_VERSION_MISMATCH"),
            ("instrument_id", "SIM:OTHER", "FENCE_INSTRUMENT_MISMATCH"),
            ("source_id", "other-source", "FENCE_SOURCE_OR_SNAPSHOT_ID_MISMATCH"),
            ("snapshot_id", "snap-2", "FENCE_SOURCE_OR_SNAPSHOT_ID_MISMATCH"),
        ):
            with self.subTest(field=key):
                found = self.evaluate(replace(self.fence, **{key: value}))
                self.assertIn(expected, found.findings)
                self.assertFalse(found.release_reservations)

    def test_source_frame_after_broker_snapshot_boundary_fails(self):
        proof = self.appended(self.fence, SourceChannel.ORDERS, "late-1", available=13)
        self.assertIn("SOURCE_EVENT_AFTER_SNAPSHOT_BOUNDARY", self.evaluate(proof).findings)

    def test_available_timestamp_reversal_within_channel_fails(self):
        first = self.appended(self.fence, SourceChannel.ORDERS, "later", available=7)
        second = self.appended(first, SourceChannel.ORDERS, "earlier", available=5)
        self.assertIn("SOURCE_AVAILABILITY_REGRESSION", self.evaluate(second).findings)

    def test_duplicate_source_event_id_on_same_channel_detected(self):
        first = self.appended(self.fence, SourceChannel.ORDERS, "reused", available=4)
        second = self.appended(first, SourceChannel.ORDERS, "reused", available=5)
        self.assertIn("DUPLICATE_SOURCE_EVENT_ID", self.evaluate(second).findings)

    def test_completeness_claim_is_not_implicitly_accepted_if_missing(self):
        self.assertIn(
            "SOURCE_COMPLETENESS_UNASSERTED",
            self.evaluate(replace(self.fence, source_claims_complete=False)).findings,
        )

    def test_paper_or_live_source_cannot_qualify_offline_fence(self):
        for environment in ("paper", "live"):
            with self.subTest(environment=environment):
                out = self.evaluate(replace(self.fence, environment=environment))
                self.assertIn("EXTERNAL_BROKER_ENVIRONMENT_UNSUPPORTED", out.findings)
                self.assertFalse(out.source_authenticated)

    def test_missing_cursor_channel_and_invalid_types_are_rejected(self):
        with self.assertRaisesRegex(LifecycleError, "all initial channel cursors"):
            replace(self.fence, anchors=anchors()[:-1])
        with self.assertRaisesRegex(LifecycleError, "immutable"):
            replace(self.fence, frames=[])
        with self.assertRaisesRegex(LifecycleError, "source frame type"):
            replace(self.fence, frames=(object(),))
        with self.assertRaisesRegex(LifecycleError, "explicit bool"):
            replace(self.fence, source_claims_complete=1)
        with self.assertRaisesRegex(LifecycleError, "frame sequence"):
            SourceFrame(SourceChannel.ORDERS, 0, "bad", 1, 2, HASH, GENESIS, GENESIS)

    def test_result_authority_cannot_be_overridden_by_constructor(self):
        result = self.evaluate()
        with self.assertRaises(TypeError):
            type(result)(account_scope="SIM_ACCOUNT", snapshot_id="snap-1", findings=(), broker_authorization=True)
        with self.assertRaises((AttributeError, TypeError)):
            result.release_reservations = True

    def test_comparator_findings_are_never_masked_by_valid_cursor_chain(self):
        report = replace(self.snapshot, position=Decimal("4"))
        matching_digest = replace(self.fence, snapshot_sha256=reported_snapshot_sha256(report))
        out = self.evaluate(matching_digest, snapshot=report)
        self.assertIn("POSITION_DISAGREES_WITH_LOCAL_FILL_PROJECTION", out.findings)
        self.assertFalse(out.internally_consistent)

    def test_import_does_not_need_nautilus_or_scientific_packages(self):
        script = (
            "import sys; from lab.kernel.source_fence import assess_fenced_report; "
            "assert 'nautilus_trader' not in sys.modules and 'numpy' not in sys.modules; print('cursor-fence-stdlib')"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "cursor-fence-stdlib")


if __name__ == "__main__":
    unittest.main()
