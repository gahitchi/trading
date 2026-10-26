"""Android-portable adversarial tests for broker-neutral offline order replay."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from lab.kernel.order_lifecycle import BrokerEvidence, LifecycleError, OfflineOrderSession
from lab.markets.domain import OrderIntent, Side
from lab.markets.journal import EventJournal

ROOT = Path(__file__).resolve().parents[1]
DIGEST = hashlib.sha256(b"synthetic broker evidence bytes").hexdigest()


def intent(key="FIRST", *, side=Side.SELL, quantity=Decimal("6"), at=1, reduce_only=True):
    return OrderIntent(
        client_order_id=key, instrument_id="SIM:TEST-SHARE", side=side,
        quantity=quantity, created_ns=at, reduce_only=reduce_only
    )


def evidence(key, *, at=10, version="v1"):
    return BrokerEvidence(
        event_id=key, source_id="offline_fixture", source_sha256=DIGEST,
        exchange_ns=at - 1, available_ns=at, action_version=version
    )


class IndependentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orders.sqlite3"

    def open(self, *, position=Decimal("10"), step=Decimal("1"), version="v1"):
        journal = EventJournal(self.path)
        return journal, OfflineOrderSession(
            journal, instrument_id="SIM:TEST-SHARE", action_version=version,
            starting_position=position, quantity_step=step,
        )

    def test_plan_is_reservation_not_broker_submission(self):
        journal, session = self.open()
        try:
            planned = session.plan(intent(), action_version="v1")
            self.assertFalse(planned.prepared)
            self.assertIsNone(planned.broker_order_id)
            self.assertEqual(planned.reserved, Decimal("6"))
            self.assertEqual(session.reserved_reduction, Decimal("6"))
            self.assertEqual(session.position_from_fills, Decimal("10"))
            self.assertFalse(session.frozen)
            self.assertEqual(journal.read_all()[1]["kind"], "intent")
            self.assertEqual(len(journal.read_all()), 2)
        finally:
            journal.close()

    def test_sizing_never_oversells_unresolved_others(self):
        journal, session = self.open()
        try:
            session.plan(intent(), action_version="v1")
            with self.assertRaisesRegex(LifecycleError, "exceed inventory"):
                session.plan(intent("SECOND", quantity=Decimal("5"), at=2), action_version="v1")
            session.plan(intent("SECOND", quantity=Decimal("4"), at=2), action_version="v1")
            with self.assertRaisesRegex(LifecycleError, "reverse"):
                session.plan(intent("WRONG", side=Side.BUY, quantity=Decimal("1"), at=3), action_version="v1")
        finally:
            journal.close()

    def test_submit_is_durable_only_and_cancel_request_stays_reserved(self):
        journal, session = self.open()
        try:
            session.plan(intent(), action_version="v1")
            result = session.prepare_dispatch("FIRST", timestamp_ns=2)
            self.assertTrue(result.prepared)
            result = session.request_cancel("FIRST", timestamp_ns=3)
            self.assertTrue(result.cancel_requested)
            self.assertEqual(session.reserved_reduction, Decimal("6"))
            self.assertEqual(result.remaining, Decimal("6"))
            self.assertEqual([x["kind"] for x in journal.read_all()], ["session", "intent", "submitted", "cancel"])
            session.assert_recovered()
            self.assertEqual(session.reserved_reduction, Decimal("6"))
        finally:
            journal.close()

    def test_partial_fills_do_not_release_remaining_cancellation_reservation(self):
        journal, session = self.open()
        try:
            session.plan(intent(), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=2)
            session.observe("FIRST", "ack", evidence("ack", at=3), broker_order_id="b-order-01")
            session.observe(
                "FIRST", "fill", evidence("fill1", at=4),
                quantity=Decimal("2"), execution_id="exec-01"
            )
            self.assertEqual(session.position_from_fills, Decimal("8"))
            self.assertEqual(session.orders["FIRST"].filled, Decimal("2"))
            self.assertEqual(session.reserved_reduction, Decimal("4"))
            session.request_cancel("FIRST", timestamp_ns=5)
            self.assertEqual(session.reserved_reduction, Decimal("4"))
            self.assertFalse(session.frozen)
        finally:
            journal.close()

    def test_partial_or_unknown_prevents_unsafe_resubmission(self):
        journal, session = self.open()
        try:
            session.plan(intent(), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=2)
            session.observe("FIRST", "unknown", evidence("timeout", at=3))
            self.assertTrue(session.frozen)
            self.assertEqual(session.reserved_reduction, Decimal("6"))
            with self.assertRaisesRegex(LifecycleError, "freezes"):
                session.plan(intent("NEXT", quantity=Decimal("1")), action_version="v1")
            with self.assertRaisesRegex(LifecycleError, "duplicate/terminal"):
                session.prepare_dispatch("FIRST", timestamp_ns=4)
        finally:
            journal.close()

    def test_cancel_confirmation_does_not_release_inventory_or_allow_new_intent(self):
        journal, session = self.open()
        try:
            session.plan(intent(), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=2)
            session.request_cancel("FIRST", timestamp_ns=3)
            session.observe("FIRST", "cancel_confirmed", evidence("cancelled", at=4))
            self.assertTrue(session.frozen)
            self.assertEqual(session.reserved_reduction, Decimal("6"))
            with self.assertRaisesRegex(LifecycleError, "freezes"):
                session.plan(intent("NEW", quantity=Decimal("1"), at=5), action_version="v1")
        finally:
            journal.close()

    def test_broker_cancel_confirmation_does_not_prevent_late_fill(self):
        journal, session = self.open()
        try:
            session.plan(intent(), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=2)
            session.request_cancel("FIRST", timestamp_ns=3)
            session.observe("FIRST", "cancel_confirmed", evidence("cancelled", at=4))
            self.assertEqual(session.orders["FIRST"].terminal, "cancelled")
            self.assertEqual(session.reserved_reduction, Decimal("6"))
            self.assertTrue(session.frozen)
            session.observe(
                "FIRST", "fill", evidence("late-fill", at=5),
                quantity=Decimal("2"), execution_id="late-exec"
            )
            self.assertEqual(session.orders["FIRST"].filled, Decimal("2"))
            self.assertEqual(session.position_from_fills, Decimal("8"))
            self.assertTrue(session.frozen)
            self.assertEqual(session.reserved_reduction, Decimal("4"))
            with self.assertRaisesRegex(LifecycleError, "freezes"):
                session.plan(intent("NEXT", quantity=Decimal("1")), action_version="v1")
        finally:
            journal.close()

    def test_receipt_duplicate_is_idempotent_but_conflict_is_rejected(self):
        journal, session = self.open()
        try:
            session.plan(intent(), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=2)
            receipt = evidence("fill-01", at=4)
            first = session.observe(
                "FIRST", "fill", receipt, quantity=Decimal("2"), execution_id="exec-01"
            )
            before = len(journal.read_all())
            duplicate = session.observe(
                "FIRST", "fill", receipt, quantity=Decimal("2"), execution_id="exec-01"
            )
            self.assertEqual(duplicate, first)
            self.assertEqual(len(journal.read_all()), before)
            with self.assertRaisesRegex(LifecycleError, "conflicting event"):
                session.observe(
                    "FIRST", "fill", receipt, quantity=Decimal("3"), execution_id="exec-01"
                )
            self.assertEqual(session.orders["FIRST"].filled, Decimal("2"))
        finally:
            journal.close()

    def test_same_execution_id_across_different_orders_is_not_two_fills(self):
        journal, session = self.open()
        try:
            session.plan(intent("FIRST", quantity=Decimal("5")), action_version="v1")
            session.plan(intent("SECOND", quantity=Decimal("5"), at=2), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=3)
            session.prepare_dispatch("SECOND", timestamp_ns=4)
            session.observe(
                "FIRST", "fill", evidence("first-fill", at=5),
                quantity=Decimal("1"), execution_id="shared-exec"
            )
            with self.assertRaisesRegex(LifecycleError, "replay"):
                session.observe(
                    "SECOND", "fill", evidence("second-fill", at=6),
                    quantity=Decimal("1"), execution_id="shared-exec"
                )
        finally:
            journal.close()

    def test_overfill_and_lot_step_are_refused_before_journaling(self):
        journal, session = self.open(step=Decimal("0.1"))
        try:
            session.plan(intent(quantity=Decimal("1.0")), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=2)
            for qty in (Decimal("1.1"), Decimal("0.11"), Decimal("NaN"), Decimal("-1")):
                with self.subTest(qty=qty), self.assertRaises(LifecycleError):
                    session.observe(
                        "FIRST", "fill", evidence("bad", at=3),
                        quantity=qty, execution_id="bad-exec"
                    )
            self.assertEqual(session.orders["FIRST"].filled, Decimal("0"))
            self.assertEqual(len(journal.read_all()), 3)
        finally:
            journal.close()

    def test_foreign_instrument_bad_version_opening_and_real_orders_rejected(self):
        journal, session = self.open()
        try:
            with self.assertRaises(LifecycleError):
                session.plan(intent(reduce_only=False), action_version="v1")
            with self.assertRaises(LifecycleError):
                session.plan(intent(side=Side.BUY), action_version="v1")
            with self.assertRaises(LifecycleError):
                session.plan(intent(), action_version="split-v2")
            session.plan(intent(), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=2)
            with self.assertRaisesRegex(LifecycleError, "version mismatch"):
                session.observe("FIRST", "ack", evidence("ack", at=3, version="v2"), broker_order_id="broker-01")
        finally:
            journal.close()

    def test_arbitrary_decimal_lot_steps_are_supported(self):
        journal, session = self.open(step=Decimal("0.25"))
        try:
            accepted = session.plan(intent(quantity=Decimal("0.75")), action_version="v1")
            self.assertEqual(accepted.reserved, Decimal("0.75"))
            with self.assertRaisesRegex(LifecycleError, "lot step"):
                session.plan(intent("BAD", quantity=Decimal("0.70"), at=2), action_version="v1")
        finally:
            journal.close()

    def test_prepare_fails_when_any_source_is_unreconciled(self):
        journal, session = self.open()
        try:
            session.plan(intent("FIRST", quantity=Decimal("5")), action_version="v1")
            session.plan(intent("SECOND", quantity=Decimal("5"), at=2), action_version="v1")
            session.prepare_dispatch("FIRST", timestamp_ns=3)
            session.observe("FIRST", "unknown", evidence("timeout", at=4))
            with self.assertRaisesRegex(LifecycleError, "freezes"):
                session.prepare_dispatch("SECOND", timestamp_ns=5)
        finally:
            journal.close()

    def test_exact_restart_recovers_all_executions_and_rejects_new_opening_position(self):
        journal, session = self.open()
        session.plan(intent(), action_version="v1")
        session.prepare_dispatch("FIRST", timestamp_ns=2)
        session.observe(
            "FIRST", "fill", evidence("fill1", at=3),
            quantity=Decimal("2"), execution_id="exec1"
        )
        before = session.state_digest()
        journal.close()
        with EventJournal(self.path) as journal:
            resumed = OfflineOrderSession(
                journal, instrument_id="SIM:TEST-SHARE", action_version="v1",
                starting_position=Decimal("10"), quantity_step=Decimal("1"),
            )
            self.assertEqual(resumed.state_digest(), before)
            self.assertEqual(resumed.reserved_reduction, Decimal("4"))
            self.assertEqual(resumed.position_from_fills, Decimal("8"))
            resumed.assert_recovered()
        with EventJournal(self.path) as journal, self.assertRaisesRegex(LifecycleError, "opening broker-state assumptions differ"):
            OfflineOrderSession(
                journal, instrument_id="SIM:TEST-SHARE", action_version="v1",
                starting_position=Decimal("11"), quantity_step=Decimal("1"),
            )

    def test_hash_valid_economic_state_forgery_fails_replay(self):
        journal, session = self.open()
        journal.append(
            "plan:FORGED", "intent", 1,
            {
                "event": {
                    "order_id": "FORGED",
                    "intent": {
                        "client_order_id": "FORGED", "instrument_id": "SIM:TEST-SHARE",
                        "side": "sell", "quantity": "1", "created_ns": 1, "reduce_only": True,
                    },
                    "action_version": "v1",
                },
                "state_sha256": "0" * 64,
            },
        )
        journal.verify()
        with self.assertRaisesRegex(LifecycleError, "state replay diverged"):
            session.recover()
        journal.close()

    def test_corrupted_replay_quarantines_the_same_instance(self):
        journal, session = self.open()
        journal.append(
            "plan:FORGED", "intent", 1,
            {
                "event": {
                    "order_id": "FORGED",
                    "intent": {
                        "client_order_id": "FORGED", "instrument_id": "SIM:TEST-SHARE",
                        "side": "sell", "quantity": "1", "created_ns": 1, "reduce_only": True,
                    },
                    "action_version": "v1",
                },
                "state_sha256": "0" * 64,
            },
        )
        with self.assertRaisesRegex(LifecycleError, "diverged"):
            session.recover()
        with self.assertRaisesRegex(LifecycleError, "quarantined"):
            session.plan(intent("NEW", at=2), action_version="v1")
        journal.close()

    def test_cannot_mix_corporate_journal_with_order_journal(self):
        journal, _session = self.open()
        journal.append("corp-event", "corporate_action", 3, {"some": "data"})
        with self.assertRaisesRegex(LifecycleError, "foreign journal event"):
            OfflineOrderSession(
                journal, instrument_id="SIM:TEST-SHARE", action_version="v1",
                starting_position=Decimal("10"), quantity_step=Decimal("1"),
            )
        journal.close()

    def test_no_third_party_packages_or_network_import_needed(self):
        statement = (
            "from lab.kernel.order_lifecycle import OfflineOrderSession; import sys; "
            "assert 'nautilus_trader' not in sys.modules and 'numpy' not in sys.modules; print('portable-oms')"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", statement],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "portable-oms")


if __name__ == "__main__":
    unittest.main()
