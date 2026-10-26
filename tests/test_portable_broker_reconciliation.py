"""Android-portable adversarial snapshot parity tests; no brokerage connection."""

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

from lab.kernel.broker_reconciliation import (
    ReportedExecution,
    ReportedOrder,
    ReportedSnapshot,
    compare_reported_snapshot,
)
from lab.kernel.order_lifecycle import BrokerEvidence, LifecycleError, OfflineOrderSession
from lab.markets.domain import OrderIntent, Side
from lab.markets.journal import EventJournal

ROOT = Path(__file__).resolve().parents[1]
HASH = hashlib.sha256(b"test broker export bytes").hexdigest()


def evidence(event_id: str, at: int) -> BrokerEvidence:
    return BrokerEvidence(
        event_id=event_id,
        account_scope="OFFLINE_TEST_ACCT",
        source_id="fixture",
        source_sha256=HASH,
        exchange_ns=at - 1,
        available_ns=at,
        action_version="v1",
    )


class PortableReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orders.sqlite3"
        self.journal = EventJournal(self.path)
        self.addCleanup(self.journal.close)
        self.session = OfflineOrderSession(
            self.journal,
            account_scope="OFFLINE_TEST_ACCT",
            instrument_id="SIM:RECON",
            action_version="v1",
            starting_position=Decimal("5"),
            quantity_step=Decimal("0.25"),
        )

    def plan(self, key: str = "A", quantity: Decimal = Decimal("3"), *, time: int = 1) -> None:
        intent = OrderIntent(
            client_order_id=key,
            instrument_id="SIM:RECON",
            side=Side.SELL,
            quantity=quantity,
            created_ns=time,
            reduce_only=True,
        )
        self.session.plan(intent, action_version="v1")

    def placed(self, *, filled: bool = True) -> None:
        self.plan()
        self.session.prepare_dispatch("A", timestamp_ns=2)
        self.session.observe("A", "ack", evidence("ack", 3), broker_order_id="BK1")
        if filled:
            self.session.observe("A", "fill", evidence("part1", 4), quantity=Decimal("1.25"), execution_id="EX1")

    def snapshot(self, *, orders: tuple | None = None, executions: tuple | None = None, **overrides):
        default_orders = (
            ReportedOrder(
                client_order_id="A",
                broker_order_id="BK1",
                status="open",
                cumulative_filled=Decimal("1.25"),
            ),
        )
        default_executions = (
            ReportedExecution(
                client_order_id="A",
                execution_id="EX1",
                quantity=Decimal("1.25"),
                exchange_ns=3,
                available_ns=4,
            ),
        )
        params = {
            "account_scope": "OFFLINE_TEST_ACCT",
            "instrument_id": "SIM:RECON",
            "action_version": "v1",
            "snapshot_id": "S1",
            "source_id": "testfile",
            "source_sha256": HASH,
            "asof_ns": 10,
            "available_ns": 11,
            "position": Decimal("3.75"),
            "orders": default_orders if orders is None else orders,
            "executions": default_executions if executions is None else executions,
            "positions_complete": True,
            "orders_complete": True,
            "executions_complete": True,
            "causal_boundary_complete": True,
        }
        params.update(overrides)
        return ReportedSnapshot(**params)

    def test_equal_reports_are_only_consistent_not_authorized(self):
        self.placed()
        before = self.session.state_digest()
        result = compare_reported_snapshot(self.session, self.snapshot())
        self.assertTrue(result.internally_consistent)
        self.assertEqual(result.findings, ())
        self.assertFalse(result.source_authenticated)
        self.assertFalse(result.broker_authorization)
        self.assertFalse(result.release_reservations)
        self.assertEqual(self.session.state_digest(), before)
        self.assertEqual(self.session.reserved_reduction, Decimal("1.75"))

    def test_reconciliation_report_cannot_be_constructed_with_authority(self):
        self.placed()
        report = compare_reported_snapshot(self.session, self.snapshot())
        self.assertFalse(report.source_authenticated)
        self.assertFalse(report.broker_authorization)
        self.assertFalse(report.release_reservations)
        with self.assertRaises(TypeError):
            type(report)(
                account_scope="OFFLINE_TEST_ACCT",
                snapshot_id="S1",
                findings=(),
                broker_authorization=True,
            )
        with self.assertRaises((AttributeError, TypeError)):
            report.release_reservations = True

    def test_reported_position_disagrees_even_if_order_history_matches(self):
        self.placed()
        outcome = compare_reported_snapshot(self.session, self.snapshot(position=Decimal("5")))
        self.assertIn("POSITION_DISAGREES_WITH_LOCAL_FILL_PROJECTION", outcome.findings)

    def test_missing_execution_and_false_cumulative_fills_both_block_parity(self):
        self.placed()
        snap = self.snapshot(
            executions=(),
            orders=(ReportedOrder("A", "BK1", "open", Decimal("0")),),
        )
        findings = compare_reported_snapshot(self.session, snap).findings
        self.assertIn("BROKER_EXECUTION_MISSING", findings)
        self.assertIn("BROKER_EXECUTION_SUM_MISMATCH", findings)
        self.assertIn("BROKER_CUMULATIVE_FILL_MISMATCH", findings)

    def test_outside_journal_execution_not_silently_imported(self):
        self.placed()
        extra = ReportedExecution("A", "EX2", Decimal("0.25"), 5, 6)
        result = compare_reported_snapshot(self.session, self.snapshot(executions=(*self.snapshot().executions, extra)))
        self.assertIn("EXTERNAL_OR_UNKNOWN_EXECUTION", result.findings)

    def test_broker_order_id_conflict_and_unknown_external_order(self):
        self.placed()
        others = (
            ReportedOrder("A", "BROKER-OTHER", "open", Decimal("1.25")),
            ReportedOrder("UNKNOWN", "BK2", "open", Decimal("0")),
        )
        findings = compare_reported_snapshot(self.session, self.snapshot(orders=others)).findings
        self.assertIn("BROKER_ORDER_ID_CONFLICT", findings)
        self.assertIn("EXTERNAL_OR_UNKNOWN_ORDER", findings)

    def test_broker_unknown_and_incomplete_coverage_never_qualify(self):
        self.placed()
        report = self.snapshot(
            orders=(ReportedOrder("A", "BK1", "unknown", Decimal("1.25")),),
            positions_complete=False,
            executions_complete=False,
            causal_boundary_complete=False,
        )
        findings = compare_reported_snapshot(self.session, report).findings
        self.assertIn("UNRESOLVED_BROKER_ORDER_STATE", findings)
        self.assertIn("POSITION_COVERAGE_UNASSERTED", findings)
        self.assertIn("EXECUTION_COVERAGE_UNASSERTED", findings)
        self.assertIn("CAUSAL_FENCE_UNASSERTED", findings)

    def test_cancel_in_flight_and_late_fill_freezes_local_parity(self):
        self.placed()
        self.session.request_cancel("A", timestamp_ns=5)
        result = compare_reported_snapshot(self.session, self.snapshot())
        self.assertIn("CANCEL_REQUEST_STILL_WORKING", result.findings)
        self.session.observe("A", "cancel_confirmed", evidence("cancelled", 6))
        cancelled = self.snapshot(orders=(ReportedOrder("A", "BK1", "cancelled", Decimal("1.25")),))
        result = compare_reported_snapshot(self.session, cancelled)
        self.assertIn("UNRESOLVED_LOCAL_FREEZE", result.findings)
        self.assertFalse(result.release_reservations)

    def test_missing_prepared_order_is_a_blocker(self):
        self.placed(filled=False)
        missing = self.snapshot(position=Decimal("5"), orders=(), executions=())
        self.assertIn(
            "BROKER_ORDER_MISSING_FROM_HISTORY",
            compare_reported_snapshot(self.session, missing).findings,
        )

    def test_unprepared_intent_must_not_appear_in_broker_history(self):
        self.plan()
        phantom = self.snapshot(
            position=Decimal("5"), orders=(ReportedOrder("A", "BK1", "open", Decimal("0")),), executions=()
        )
        self.assertIn(
            "UNSUBMITTED_INTENT_AT_BROKER",
            compare_reported_snapshot(self.session, phantom).findings,
        )

    def test_stale_broker_asof_or_incompatible_split_version_block(self):
        self.placed()
        stale = self.snapshot(asof_ns=3)
        self.assertIn(
            "SNAPSHOT_BEFORE_LOCAL_EVIDENCE",
            compare_reported_snapshot(self.session, stale).findings,
        )
        mismatched = self.snapshot(action_version="v2")
        self.assertIn(
            "SCOPE_OR_ACTION_VERSION_MISMATCH",
            compare_reported_snapshot(self.session, mismatched).findings,
        )

    def test_duplicate_order_or_exec_identity_is_not_reported_as_complete(self):
        self.placed()
        a = self.snapshot()
        with self.assertRaisesRegex(LifecycleError, "duplicate broker client"):
            replace(a, orders=(a.orders[0], a.orders[0]))
        with self.assertRaisesRegex(LifecycleError, "duplicate broker execution"):
            replace(a, executions=(a.executions[0], a.executions[0]))
        with self.assertRaisesRegex(LifecycleError, "immutable"):
            replace(a, orders=list(a.orders))
        with self.assertRaises(LifecycleError):
            replace(a, position=float("nan"))
        with self.assertRaisesRegex(LifecycleError, "coverage assertions"):
            replace(a, positions_complete=1)

    def test_future_knowledge_and_invalid_source_time_rejected(self):
        self.placed()
        a = self.snapshot()
        with self.assertRaisesRegex(LifecycleError, "outside snapshot"):
            replace(a, asof_ns=2)
        with self.assertRaisesRegex(LifecycleError, "before its as-of"):
            replace(a, available_ns=5)
        with self.assertRaisesRegex(LifecycleError, "SHA-256"):
            replace(a, source_sha256="untrusted")

    def test_duplicate_execution_wrong_order_and_quantity_discrepancy(self):
        self.placed()
        wrong = ReportedExecution("B", "EX1", Decimal("1.25"), 3, 4)
        errors = compare_reported_snapshot(self.session, self.snapshot(executions=(wrong,))).findings
        self.assertIn("BROKER_EXECUTION_WRONG_ORDER", errors)
        altered = ReportedExecution("A", "EX1", Decimal("0.75"), 3, 4)
        errors = compare_reported_snapshot(self.session, self.snapshot(executions=(altered,))).findings
        self.assertIn("BROKER_EXECUTION_SUM_MISMATCH", errors)

    def test_competing_session_stale_head_is_reported(self):
        self.placed()
        with EventJournal(self.path) as second:
            other = OfflineOrderSession(
                second,
                account_scope="OFFLINE_TEST_ACCT",
                instrument_id="SIM:RECON",
                action_version="v1",
                starting_position=Decimal("5"),
                quantity_step=Decimal("0.25"),
            )
            other.plan(
                OrderIntent("B", "SIM:RECON", Side.SELL, Decimal("1"), 5, True),
                action_version="v1",
            )
        findings = compare_reported_snapshot(self.session, self.snapshot()).findings
        self.assertIn("LOCAL_JOURNAL_STALE", findings)

    def test_stdlib_only_no_native_engine_or_market_adapter(self):
        command = (
            "import sys; from lab.kernel.broker_reconciliation import ReportedSnapshot; "
            "assert 'nautilus_trader' not in sys.modules and 'numpy' not in sys.modules; "
            "print('offline-parity-only')"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", command],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "offline-parity-only")


if __name__ == "__main__":
    unittest.main()
