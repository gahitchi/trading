"""Portable F1 foundation — corporate cash settlement without fake fills.

All tests use Python's standard library and the SAME independent reference
accounting as the desktop branch. They do not certify Nautilus native parity.
"""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from decimal import Decimal

from lab.markets.corporate_actions import (
    CashReceipt,
    CashTermination,
    CorporateActionError,
    CorporateBook,
    ReconciledBook,
    receive,
    recognize,
)

SRC_HASH = hashlib.sha256(b"explicit source evidence bytes").hexdigest()
CASH_HASH = hashlib.sha256(b"broker-reported settlement receipt").hexdigest()


def action(**updates):
    attrs = {
        "action_id": "ca:2026:merger",
        "instrument_id": "SIM:DELISTED",
        "currency": "USD",
        "kind": "cash_merger",
        "cash_per_unit": Decimal("50.00"),
        "effective_ns": 100,
        "available_ns": 130,
        "payable_not_before_ns": 200,
        "source_id": "vendor:action:1",
        "source_sha256": SRC_HASH,
        "action_version": "v1",
    }
    attrs.update(updates)
    return CashTermination(**attrs)


def book(**updates):
    attrs = {
        "instrument_id": "SIM:DELISTED",
        "currency": "USD",
        "quantum": Decimal("0.01"),
        "position": Decimal("10"),
        "signed_basis": Decimal("400.00"),
        "cash": Decimal("1000.00"),
    }
    attrs.update(updates)
    return CorporateBook(**attrs)


def readiness(**updates):
    attrs = {
        "position": Decimal("10"),
        "positions_verified": True,
        "orders_verified": True,
        "open_order_ids": (),
        "action_version": "v1",
    }
    attrs.update(updates)
    return ReconciledBook(**attrs)


def receipt(**updates):
    attrs = {
        "receipt_id": "r1",
        "action_id": "ca:2026:merger",
        "signed_amount": Decimal("200.00"),
        "available_ns": 201,
        "source_id": "broker:cash:001",
        "source_sha256": CASH_HASH,
    }
    attrs.update(updates)
    return CashReceipt(**attrs)


class CorporateActionPortableTests(unittest.TestCase):
    def test_recognition_books_receivable_not_cash_or_trade(self):
        start = book()
        arrived = recognize(start, action(), readiness(), observed_ns=130)
        self.assertEqual(start.position, Decimal("10"))
        self.assertEqual(arrived.position, Decimal("0"))
        self.assertEqual(arrived.signed_basis, Decimal("0"))
        self.assertEqual(arrived.cash, start.cash)
        self.assertEqual(arrived.pending_cash, Decimal("500.00"))
        self.assertEqual(arrived.realized_pnl, Decimal("100.00"))
        self.assertEqual(arrived.receipt_digests, ())
        self.assertEqual(len(arrived.action_digest), 64)
        with self.assertRaisesRegex(CorporateActionError, "already recognized"):
            recognize(arrived, action(), readiness(), observed_ns=131)

    def test_deferred_partial_settlement_and_idempotent_duplicate(self):
        committed = recognize(book(), action(), readiness(), observed_ns=150)
        with self.assertRaisesRegex(CorporateActionError, "earliest"):
            receive(committed, receipt(available_ns=199), observed_ns=200)
        with self.assertRaisesRegex(CorporateActionError, "not available"):
            receive(committed, receipt(), observed_ns=200)
        first = receive(committed, receipt(), observed_ns=201)
        self.assertEqual(first.cash, Decimal("1200.00"))
        self.assertEqual(first.pending_cash, Decimal("300.00"))
        self.assertEqual(first.realized_pnl, Decimal("100.00"))
        self.assertIs(receive(first, receipt(), observed_ns=202), first)
        final = receive(
            first, receipt(receipt_id="r2", signed_amount=Decimal("300.00"), available_ns=210), observed_ns=210
        )
        self.assertEqual(final.cash, Decimal("1500.00"))
        self.assertEqual(final.pending_cash, Decimal("0.00"))
        self.assertEqual(len(final.receipt_digests), 2)
        with self.assertRaisesRegex(CorporateActionError, "outstanding payable"):
            receive(final, receipt(receipt_id="r3", signed_amount=Decimal("0.01")), observed_ns=300)

    def test_short_merger_creates_payable_and_realized_loss_not_short_sale(self):
        start = book(position=Decimal("-10"), signed_basis=Decimal("-400.00"))
        approved = recognize(start, action(), readiness(position=Decimal("-10")), observed_ns=150)
        self.assertEqual(approved.pending_cash, Decimal("-500.00"))
        self.assertEqual(approved.cash, start.cash)
        self.assertEqual(approved.realized_pnl, Decimal("-100.00"))
        first = receive(approved, receipt(signed_amount=Decimal("-200.00")), observed_ns=201)
        self.assertEqual(first.pending_cash, Decimal("-300.00"))
        self.assertEqual(first.cash, Decimal("800.00"))
        second = receive(
            first, receipt(receipt_id="r2", signed_amount=Decimal("-300.00"), available_ns=202), observed_ns=202
        )
        self.assertEqual(second.cash, Decimal("500.00"))
        self.assertEqual(second.pending_cash, Decimal("0.00"))
        with self.assertRaisesRegex(CorporateActionError, "direction"):
            receive(first, receipt(receipt_id="bad", signed_amount=Decimal("100.00")), observed_ns=203)

    def test_zero_recovery_writeoff_long_and_short_are_opposite(self):
        zero = action(
            kind="bankruptcy_writeoff",
            cash_per_unit=Decimal("0"),
            payable_not_before_ns=100,
        )
        for held, basis, expected in (
            (Decimal("10"), Decimal("400.00"), Decimal("-400.00")),
            (Decimal("-10"), Decimal("-400.00"), Decimal("400.00")),
        ):
            with self.subTest(held=held):
                prior = book(position=held, signed_basis=basis)
                out = recognize(prior, zero, readiness(position=held), observed_ns=130)
                self.assertEqual(out.position, 0)
                self.assertEqual(out.pending_cash, 0)
                self.assertEqual(out.cash, prior.cash)
                self.assertEqual(out.realized_pnl, expected)
                with self.assertRaisesRegex(CorporateActionError, "outstanding payable"):
                    receive(out, receipt(receipt_id="never"), observed_ns=201)

    def test_unknown_gtc_partial_cancel_and_version_freeze_recognition(self):
        for change in (
            {"positions_verified": False},
            {"orders_verified": False},
            {"open_order_ids": ("GTC:pending-cancel",)},
            {"position": Decimal("9.5")},
            {"action_version": "before-split"},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(CorporateActionError, "unreconciled"):
                recognize(book(), action(), readiness(**change), observed_ns=150)

    def test_bitemporal_availability_and_stale_observation(self):
        with self.assertRaisesRegex(CorporateActionError, "future"):
            recognize(book(), action(), readiness(), observed_ns=129)
        with self.assertRaisesRegex(CorporateActionError, "regressed"):
            recognize(book(last_observed_ns=190), action(), readiness(), observed_ns=170)
        newer = recognize(book(), action(), readiness(), observed_ns=155)
        with self.assertRaisesRegex(CorporateActionError, "regressed"):
            receive(newer, receipt(), observed_ns=154)
        with self.assertRaisesRegex(CorporateActionError, "not available"):
            receive(newer, receipt(), observed_ns=199)

    def test_fractional_entitlement_never_silently_rounded(self):
        prior = book(position=Decimal("0.333"), signed_basis=Decimal("1.00"))
        a = action(cash_per_unit=Decimal("1.00"))
        with self.assertRaisesRegex(CorporateActionError, "cash-in-lieu"):
            recognize(prior, a, readiness(position=Decimal("0.333")), observed_ns=150)
        self.assertEqual(prior.position, Decimal("0.333"))

    def test_duplicate_receipt_conflicts_and_overpayment(self):
        open_book = recognize(book(), action(), readiness(), observed_ns=150)
        first = receive(open_book, receipt(), observed_ns=201)
        with self.assertRaisesRegex(CorporateActionError, "conflicting"):
            receive(first, receipt(signed_amount=Decimal("201.00")), observed_ns=202)
        with self.assertRaisesRegex(CorporateActionError, "exceeds unsettled"):
            receive(first, receipt(receipt_id="over", signed_amount=Decimal("300.01")), observed_ns=203)
        with self.assertRaisesRegex(CorporateActionError, "currency precision"):
            receive(first, receipt(receipt_id="fraction", signed_amount=Decimal("0.001")), observed_ns=203)

    def test_unsupported_broker_semantics_are_not_guessed(self):
        for kwargs in (
            {"kind": "unknown"},
            {"kind": "cash_merger", "cash_per_unit": Decimal("0")},
            {"kind": "bankruptcy_writeoff", "cash_per_unit": Decimal("0.0001")},
            {"cash_per_unit": Decimal("-1")},
            {"cash_per_unit": Decimal("NaN")},
            {"source_sha256": "not a hash"},
            {"available_ns": -1},
            {"payable_not_before_ns": 99},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(CorporateActionError):
                action(**kwargs)
        with self.assertRaises(CorporateActionError):
            book(position=Decimal("0"), signed_basis=Decimal("1.00"))
        with self.assertRaises(CorporateActionError):
            book(position=Decimal("-1"), signed_basis=Decimal("1.00"))
        with self.assertRaises(CorporateActionError):
            book(quantum=Decimal("0.03"))
        with self.assertRaisesRegex(CorporateActionError, "immutable"):
            book(receipt_digests=[])
        with self.assertRaises(CorporateActionError):
            receipt(signed_amount=Decimal("NaN"))

    def test_genuine_zero_basis_equity_is_not_assumed_corrupt(self):
        prior = book(signed_basis=Decimal("0.00"))
        settled = recognize(prior, action(), readiness(), observed_ns=130)
        self.assertEqual(settled.realized_pnl, Decimal("500.00"))
        self.assertEqual(settled.pending_cash, Decimal("500.00"))
        self.assertEqual(settled.cash, prior.cash)

    def test_json_replay_rejects_float_coercion_and_string_order_lists(self):
        action_record = action().record()
        action_record["cash_per_unit"] = 50.0
        with self.assertRaisesRegex(CorporateActionError, "exact decimal string"):
            CashTermination.from_record(action_record)
        receipt_record = receipt().record()
        receipt_record["signed_amount"] = 200.0
        with self.assertRaisesRegex(CorporateActionError, "exact decimal string"):
            CashReceipt.from_record(receipt_record)
        recon_record = readiness().record()
        recon_record["open_order_ids"] = "oops"
        with self.assertRaisesRegex(CorporateActionError, "orders a list"):
            ReconciledBook.from_record(recon_record)

    def test_wrong_instrument_receipt_action_or_currency_refused(self):
        for kwargs in ({"instrument_id": "SIM:OTHER"}, {"currency": "EUR"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(CorporateActionError, "differs"):
                recognize(book(), action(**kwargs), readiness(), observed_ns=150)
        opened = recognize(book(), action(), readiness(), observed_ns=150)
        with self.assertRaisesRegex(CorporateActionError, "not bound"):
            receive(opened, receipt(action_id="some-other-action"), observed_ns=201)

    def test_canonical_roundtrip_and_sign_constraints(self):
        a = action()
        r = readiness()
        cash = receipt()
        self.assertEqual(CashTermination.from_record(a.record()), a)
        self.assertEqual(ReconciledBook.from_record(r.record()), r)
        self.assertEqual(CashReceipt.from_record(cash.record()), cash)
        with self.assertRaises(CorporateActionError):
            CashReceipt.from_record({**cash.record(), "unexpected": "x"})
        self.assertNotEqual(action(source_sha256=CASH_HASH).record(), a.record())
        self.assertEqual(replace(book(), cash=Decimal("900.00")).cash, Decimal("900.00"))


if __name__ == "__main__":
    unittest.main()
