"""Crash and adversarial receipt tests for the independent corporate-action journal."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from lab.markets.corporate_actions import (
    CashReceipt,
    CashTermination,
    CorporateActionError,
    CorporateBook,
    ReconciledBook,
    _digest,
    recognize,
)
from lab.markets.corporate_journal import CorporateSession
from lab.markets.journal import EventJournal, JournalError

REPO = Path(__file__).resolve().parents[1]
SHA = hashlib.sha256(b"sample vendor data").hexdigest()


def initial(position=Decimal("10"), signed_basis=Decimal("400.00")):
    return CorporateBook(
        instrument_id="SIM:DELISTED",
        currency="USD",
        quantum=Decimal("0.01"),
        position=position,
        signed_basis=signed_basis,
        cash=Decimal("1000.00"),
    )


def action(kind="cash_merger", price=Decimal("50.00")):
    return CashTermination(
        action_id="corp_001",
        instrument_id="SIM:DELISTED",
        currency="USD",
        kind=kind,
        cash_per_unit=price,
        effective_ns=10,
        available_ns=20,
        payable_not_before_ns=30,
        source_id="vendor_1",
        source_sha256=SHA,
        action_version="v1",
    )


def readiness(position=Decimal("10")):
    return ReconciledBook(
        position=position,
        positions_verified=True,
        orders_verified=True,
        open_order_ids=(),
        action_version="v1",
    )


def receipt(amount=Decimal("200.00"), rid="rcpt001", at=31):
    return CashReceipt(
        receipt_id=rid,
        action_id="corp_001",
        signed_amount=amount,
        available_ns=at,
        source_id="broker_cash",
        source_sha256=SHA,
    )


class PortableCorporateJournalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "corp.sqlite3"

    def test_long_partial_payments_survive_restart_and_duplicate_delivery(self):
        original = initial()
        with EventJournal(self.path) as journal:
            session = CorporateSession(journal, original)
            session.recognize(action(), readiness(), observed_ns=20)
            session.receive(receipt(), observed_ns=31)
            self.assertEqual(len(journal.read_all()), 3)
            self.assertEqual(session.book.cash, Decimal("1200.00"))
            session.assert_recovered()
        with EventJournal(self.path) as journal:
            recovered = CorporateSession(journal, original)
            self.assertEqual(recovered.book.cash, Decimal("1200.00"))
            self.assertEqual(recovered.book.pending_cash, Decimal("300.00"))
            before = journal.read_all()
            self.assertEqual(recovered.receive(receipt(), observed_ns=32), recovered.book)
            self.assertEqual(journal.read_all(), before)
            recovered.receive(receipt(amount=Decimal("300.00"), rid="rcpt002", at=32), observed_ns=32)
            self.assertEqual(recovered.book.cash, Decimal("1500.00"))
            self.assertEqual(recovered.book.pending_cash, Decimal("0.00"))
            recovered.assert_recovered()
        with EventJournal(self.path) as journal:
            self.assertEqual(CorporateSession(journal, original).book.cash, Decimal("1500.00"))
            self.assertEqual(len(journal.read_all()), 4)

    def test_short_pending_liability_and_debits_replay(self):
        start = initial(position=Decimal("-10"), signed_basis=Decimal("-400.00"))
        with EventJournal(self.path) as journal:
            current = CorporateSession(journal, start)
            current.recognize(action(), readiness(Decimal("-10")), observed_ns=20)
            self.assertEqual(current.book.pending_cash, Decimal("-500.00"))
            self.assertEqual(current.book.realized_pnl, Decimal("-100.00"))
            current.receive(receipt(Decimal("-200.00")), observed_ns=31)
            current.assert_recovered()
        with EventJournal(self.path) as journal:
            current = CorporateSession(journal, start)
            self.assertEqual(current.book.pending_cash, Decimal("-300.00"))
            self.assertEqual(current.book.cash, Decimal("800.00"))

    def test_zero_bankruptcy_writeoff_is_single_nonfill_action(self):
        with EventJournal(self.path) as journal:
            current = CorporateSession(journal, initial())
            current.recognize(action("bankruptcy_writeoff", Decimal("0")), readiness(), observed_ns=20)
            self.assertEqual(current.book.cash, Decimal("1000.00"))
            self.assertEqual(current.book.realized_pnl, Decimal("-400.00"))
            self.assertEqual(current.book.pending_cash, Decimal("0"))
            self.assertEqual([e["kind"] for e in journal.read_all()], ["session", "corporate_action"])
            current.assert_recovered()

    def test_completely_wrong_initial_account_refuses_recovery(self):
        with EventJournal(self.path) as journal:
            CorporateSession(journal, initial())
        with (
            EventJournal(self.path) as journal,
            self.assertRaisesRegex(CorporateActionError, "identity/account assumptions"),
        ):
            CorporateSession(journal, initial(signed_basis=Decimal("500.00")))

    def test_unknown_fill_after_settlement_is_not_dressed_as_corporate_cash(self):
        start = initial()
        with EventJournal(self.path) as journal:
            session = CorporateSession(journal, start)
            session.recognize(action(), readiness(), observed_ns=20)
            journal.append("pretend-fill", "fill", 21, {"price": "0.0001", "quantity": "10"})
            with self.assertRaisesRegex(CorporateActionError, "foreign order/fill/cash"):
                CorporateSession(journal, start)

    def test_bad_accounting_digest_detected_even_with_intact_journal_chain(self):
        with EventJournal(self.path) as journal:
            CorporateSession(journal, initial())
            journal.append(
                "corp-action:" + hashlib.sha256(b"corp_001").hexdigest(),
                "corporate_action",
                20,
                {
                    "action": action().record(),
                    "reconciliation": readiness().record(),
                    "book_digest": "0" * 64,
                },
            )
            journal.verify()  # The bytes are intact; the claimed arithmetic is not.
            with self.assertRaisesRegex(CorporateActionError, "diverged"):
                CorporateSession(journal, initial())

    def test_recovery_after_durable_append_before_memory_assignment(self):
        with EventJournal(self.path) as journal:
            running = CorporateSession(journal, initial())
            next_book = recognize(initial(), action(), readiness(), observed_ns=20)
            journal.append(
                "corp-action:" + hashlib.sha256(b"corp_001").hexdigest(),
                "corporate_action",
                20,
                {
                    "action": action().record(),
                    "reconciliation": readiness().record(),
                    "book_digest": _digest(next_book.record()),
                },
            )
            with self.assertRaisesRegex(JournalError, "disagrees"):
                running.assert_recovered()
        with EventJournal(self.path) as journal:
            self.assertEqual(CorporateSession(journal, initial()).book.pending_cash, Decimal("500.00"))

    def test_actual_journal_hash_tampering_fails_integrity_check(self):
        with EventJournal(self.path) as journal:
            session = CorporateSession(journal, initial())
            session.recognize(action(), readiness(), observed_ns=20)
        # Adversarially drop ordinary SQLite triggers; hash chain must *still* fail.
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET payload=? WHERE sequence=2", ('{"forged":true}',))
        with self.assertRaises(JournalError):
            EventJournal(self.path)

    def test_python_no_site_packages_corporate_replay(self):
        program = (
            "from lab.markets.corporate_actions import CorporateBook; "
            "from lab.markets.corporate_journal import CorporateSession; "
            "from lab.markets.journal import EventJournal; "
            "import sys; assert 'numpy' not in sys.modules; print('portable-corporate-journal')"
        )
        test = subprocess.run(
            [sys.executable, "-S", "-c", program],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            cwd=REPO,
            check=False,
        )
        self.assertEqual(test.returncode, 0, test.stderr)
        self.assertEqual(test.stdout.strip(), "portable-corporate-journal")


if __name__ == "__main__":
    unittest.main()
