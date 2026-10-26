"""Portable independently checked double-entry economic conservation invariants."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from lab.kernel.double_entry import (
    Account,
    AccountingInvariantError,
    Posting,
    PostingBatch,
    transition,
    verify_record,
)
from lab.markets.corporate_actions import (
    CashReceipt,
    CashTermination,
    CorporateBook,
    ReconciledBook,
    receive,
    recognize,
)
from lab.markets.corporate_journal import CorporateSession, _financial
from lab.markets.journal import EventJournal

ROOT = Path(__file__).resolve().parents[1]
SOURCE_HASH = hashlib.sha256(b"independent-vendor-evidence").hexdigest()


def book(*, held=Decimal("10"), basis=Decimal("400.00")):
    return CorporateBook(
        instrument_id="SIM:SHARE",
        currency="USD",
        quantum=Decimal("0.01"),
        position=held,
        signed_basis=basis,
        cash=Decimal("1000.00"),
    )


def action(*, cash_per_unit=Decimal("50.00"), kind="cash_merger"):
    return CashTermination(
        action_id="verified-action",
        instrument_id="SIM:SHARE",
        currency="USD",
        kind=kind,
        cash_per_unit=cash_per_unit,
        effective_ns=1,
        available_ns=2,
        payable_not_before_ns=3,
        source_id="independent-source",
        source_sha256=SOURCE_HASH,
        action_version="v1",
    )


def reconciliation(held=Decimal("10")):
    return ReconciledBook(
        position=held,
        positions_verified=True,
        orders_verified=True,
        open_order_ids=(),
        action_version="v1",
    )


def payment(amount=Decimal("150.00")):
    return CashReceipt(
        receipt_id="broker-receipt",
        action_id="verified-action",
        signed_amount=amount,
        available_ns=4,
        source_id="independent-broker-statement",
        source_sha256=SOURCE_HASH,
    )


class PortableDoubleEntryTests(unittest.TestCase):
    def test_long_merger_balances_claim_security_basis_and_realized_earnings(self):
        before = book()
        after = recognize(before, action(), reconciliation(), observed_ns=2)
        batch = transition(_financial(before), _financial(after), kind="corporate_action")
        self.assertEqual(
            [(e.account, e.debit) for e in batch.entries],
            [
                (Account.SECURITY_BASIS, Decimal("-400.00")),
                (Account.CORPORATE_CLAIM, Decimal("500.00")),
                (Account.REALIZED_EARNINGS, Decimal("-100.00")),
            ],
        )
        self.assertEqual(sum((p.debit for p in batch.entries), Decimal(0)), 0)
        self.assertEqual(batch.currency, "USD")
        self.assertEqual(batch.closing_state_sha256, _financial(after).fingerprint)
        self.assertEqual(batch.record()["version"], 1)
        self.assertEqual(verify_record(batch.record(), _financial(before), _financial(after), kind="corporate_action"), batch)

    def test_short_merger_keeps_payable_as_negative_claim(self):
        before = book(held=Decimal("-10"), basis=Decimal("-400.00"))
        after = recognize(before, action(), reconciliation(Decimal("-10")), observed_ns=2)
        batch = transition(_financial(before), _financial(after), kind="corporate_action")
        self.assertEqual(
            [(e.account, e.debit) for e in batch.entries],
            [
                (Account.SECURITY_BASIS, Decimal("400.00")),
                (Account.CORPORATE_CLAIM, Decimal("-500.00")),
                (Account.REALIZED_EARNINGS, Decimal("100.00")),
            ],
        )
        self.assertEqual(after.cash, before.cash)
        self.assertEqual(_financial(after).owner_capital, _financial(before).owner_capital)

    def test_actual_payment_changes_cash_and_reduces_claim_only(self):
        original = book()
        accrued = recognize(original, action(), reconciliation(), observed_ns=2)
        received = receive(accrued, payment(), observed_ns=4)
        batch = transition(_financial(accrued), _financial(received), kind="corporate_cash")
        self.assertEqual(
            [(entry.account, entry.debit) for entry in batch.entries],
            [(Account.CASH, Decimal("150.00")), (Account.CORPORATE_CLAIM, Decimal("-150.00"))],
        )
        self.assertEqual(received.realized_pnl, accrued.realized_pnl)
        self.assertEqual(_financial(received).owner_capital, _financial(accrued).owner_capital)

    def test_bankruptcy_zero_basis_can_retire_without_fictitious_cash_posting(self):
        previous = book(basis=Decimal("0.00"))
        zero = action(kind="bankruptcy_writeoff", cash_per_unit=Decimal("0"))
        retired = recognize(previous, zero, reconciliation(), observed_ns=2)
        entry = transition(_financial(previous), _financial(retired), kind="corporate_action")
        self.assertEqual(entry.entries, ())
        self.assertEqual(previous.cash, retired.cash)
        self.assertEqual(retired.position, 0)

    def test_zero_recovery_long_loss_and_short_gain_balance(self):
        zero = action(kind="bankruptcy_writeoff", cash_per_unit=Decimal("0"))
        for held, basis in ((Decimal("10"), Decimal("400.00")), (Decimal("-10"), Decimal("-400.00"))):
            with self.subTest(held=held):
                before = book(held=held, basis=basis)
                after = recognize(before, zero, reconciliation(held), observed_ns=2)
                batch = transition(_financial(before), _financial(after), kind="corporate_action")
                self.assertEqual(sum((entry.debit for entry in batch.entries), Decimal(0)), 0)
                self.assertEqual([entry.account for entry in batch.entries], [Account.SECURITY_BASIS, Account.REALIZED_EARNINGS])
                self.assertEqual(after.pending_cash, 0)

    def test_unbalanced_cash_credit_or_pnl_injection_fails(self):
        before = _financial(book())
        for faulty in (
            replace(before, cash=before.cash + Decimal("100.00")),
            replace(before, realized_earnings=before.realized_earnings - Decimal("100.00")),
            replace(before, corporate_claim=Decimal("50.00")),
        ):
            with self.subTest(faulty=faulty), self.assertRaisesRegex(AccountingInvariantError, "conservation"):
                transition(before, faulty, kind="corporate_action")

    def test_receipt_cannot_change_cost_basis_or_realized_earnings(self):
        before = _financial(book())
        faulty = replace(before, security_basis=Decimal("0"), corporate_claim=Decimal("400.00"))
        with self.assertRaisesRegex(AccountingInvariantError, "corporate receipt changes only"):
            transition(before, faulty, kind="corporate_cash")

    def test_no_implicit_currency_fx_or_coercive_float(self):
        before = _financial(book())
        with self.assertRaisesRegex(AccountingInvariantError, "multi-currency"):
            transition(before, replace(before, currency="EUR"), kind="corporate_action")
        for field in ("cash", "security_basis", "corporate_claim", "realized_earnings"):
            with self.subTest(field=field), self.assertRaises(AccountingInvariantError):
                replace(before, **{field: 2.0})
        with self.assertRaises(AccountingInvariantError):
            replace(before, quantum=Decimal("0.03"))

    def test_canonical_record_rejects_forged_zero_sum_postings(self):
        before = book()
        after = recognize(before, action(), reconciliation(), observed_ns=2)
        prior, later = _financial(before), _financial(after)
        valid = transition(prior, later, kind="corporate_action").record()
        forged = json.loads(json.dumps(valid))
        forged["entries"][0]["debit"] = "-399.00"
        forged["entries"][-1]["debit"] = "-101.00"
        self.assertEqual(sum(Decimal(entry["debit"]) for entry in forged["entries"]), 0)
        with self.assertRaisesRegex(AccountingInvariantError, "do not match"):
            verify_record(forged, prior, later, kind="corporate_action")
        with self.assertRaises(AccountingInvariantError):
            verify_record({**valid, "currency": "EUR"}, prior, later, kind="corporate_action")
        with self.assertRaises(AccountingInvariantError):
            verify_record({**valid, "closing_state_sha256": "0" * 64}, prior, later, kind="corporate_action")

    def test_postings_are_unique_immutable_and_balanced(self):
        closing = "0" * 64
        with self.assertRaises(AccountingInvariantError):
            PostingBatch(
                kind="corporate_cash",
                currency="USD",
                entries=(Posting(Account.CASH, Decimal("50")),),
                closing_state_sha256=closing,
            )
        with self.assertRaises(AccountingInvariantError):
            PostingBatch(
                kind="corporate_cash",
                currency="USD",
                entries=(Posting(Account.CASH, Decimal("50")), Posting(Account.CASH, Decimal("-50"))),
                closing_state_sha256=closing,
            )
        with self.assertRaises(AccountingInvariantError):
            Posting(Account.CASH, Decimal("0"))

    def test_single_sqlite_transaction_contains_economic_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "financial.sqlite3"
            opening = book()
            with EventJournal(path) as journal:
                corporate = CorporateSession(journal, opening)
                corporate.recognize(action(), reconciliation(), observed_ns=2)
                corporate.receive(payment(), observed_ns=4)
                journal.verify()
                events = journal.read_all()
                self.assertEqual(events[0]["payload"]["schema"], 2)
                self.assertEqual(events[0]["payload"]["opening_capital"], "-1400.00")
                self.assertEqual(len(events), 3)
                for event in events[1:]:
                    self.assertIn("postings", event["payload"])
                    self.assertEqual(event["payload"]["postings"]["kind"], event["kind"])
            with EventJournal(path) as journal:
                restored = CorporateSession(journal, opening)
                self.assertEqual(restored.book.cash, Decimal("1150.00"))
                self.assertEqual(restored.book.pending_cash, Decimal("350.00"))
                restored.assert_recovered()

    def test_kernel_imports_with_python_no_site_packages(self):
        script = (
            "import sys; from lab.kernel.double_entry import AccountingState, transition; "
            "assert 'nautilus_trader' not in sys.modules and 'numpy' not in sys.modules; "
            "print('independent-ledger-stdlib')"
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
        self.assertEqual(result.stdout.strip(), "independent-ledger-stdlib")


if __name__ == "__main__":
    unittest.main()
