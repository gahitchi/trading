"""Portable reference-only safety tests: no pytest, Nautilus, broker or network."""

from __future__ import annotations

import unittest
from decimal import Decimal

from lab.order_quantity import UnreconciledReduction, capped_reduction


def case(**override):
    state = {
        "broker_position": Decimal("10"),
        "requested_change": Decimal("-10"),
        "lot_step": Decimal("1"),
        "unsettled_reduction": Decimal("0"),
        "broker_position_reconciled": True,
        "broker_orders_reconciled": True,
        "instrument_definition_reconciled": True,
        "pending_increase_absent": True,
        "expected_action_version": "pre-action",
        "broker_action_version": "pre-action",
    }
    state.update(override)
    return state


class ReductionSafetyTests(unittest.TestCase):
    def test_fractional_positive_lot_truncates_toward_zero(self):
        self.assertEqual(
            capped_reduction(**case(broker_position="2.75", requested_change="-2.75", lot_step="1")),
            Decimal("-2"),
        )

    def test_negative_position_and_fractional_step_are_symmetric(self):
        self.assertEqual(
            capped_reduction(
                **case(
                    broker_position="-3.1415",
                    requested_change="3.1415",
                    lot_step="0.01",
                )
            ),
            Decimal("3.14"),
        )

    def test_untradeable_fractional_stub_is_not_rounded_up(self):
        self.assertEqual(
            capped_reduction(**case(broker_position="-0.009", requested_change="0.009", lot_step="0.01")),
            Decimal("0"),
        )

    def test_unconfirmed_cancel_reserves_full_remaining_sell(self):
        self.assertEqual(
            capped_reduction(**case(unsettled_reduction="10")),
            Decimal(0),
        )
        self.assertEqual(
            capped_reduction(**case(unsettled_reduction="4.2", lot_step="1")),
            Decimal("-5"),
        )

    def test_overreserved_position_is_an_inconsistent_broker_state(self):
        with self.assertRaises(UnreconciledReduction):
            capped_reduction(**case(unsettled_reduction="10.1"))

    def test_reversal_or_wrong_side_is_never_classified_as_reduction(self):
        for change in ("1", "0.1", "999"):
            with self.subTest(change=change), self.assertRaises(UnreconciledReduction):
                capped_reduction(**case(requested_change=change))
        self.assertEqual(capped_reduction(**case(requested_change="0")), Decimal(0))

    def test_split_requires_updated_broker_snapshot_and_order_reconciliation(self):
        with self.assertRaisesRegex(UnreconciledReduction, "corporate-action"):
            capped_reduction(
                **case(
                    broker_position="1.25",
                    requested_change="-1.25",
                    expected_action_version="split-2026-10-26",
                    broker_action_version="pre-action",
                )
            )
        with self.assertRaisesRegex(UnreconciledReduction, "reconciled"):
            capped_reduction(
                **case(
                    expected_action_version="split-2026-10-26",
                    broker_action_version="split-2026-10-26",
                    broker_orders_reconciled=False,
                )
            )
        self.assertEqual(
            capped_reduction(
                **case(
                    broker_position="1.25",
                    requested_change="-1.25",
                    lot_step="0.1",
                    expected_action_version="split-2026-10-26",
                    broker_action_version="split-2026-10-26",
                )
            ),
            Decimal("-1.2"),
        )

    def test_requires_all_broker_evidence_not_just_good_market_price(self):
        for field in (
            "broker_position_reconciled",
            "broker_orders_reconciled",
            "instrument_definition_reconciled",
            "pending_increase_absent",
        ):
            with self.subTest(field=field), self.assertRaises(UnreconciledReduction):
                capped_reduction(**case(**{field: False}))

    def test_rejects_float_nan_bad_step_and_unknown_versions(self):
        for fields in (
            {"broker_position": 3.5},
            {"lot_step": "0"},
            {"lot_step": "nan"},
            {"unsettled_reduction": "-0.1"},
            {"expected_action_version": ""},
            {"broker_action_version": ""},
        ):
            with self.subTest(fields=fields), self.assertRaises(UnreconciledReduction):
                capped_reduction(**case(**fields))


if __name__ == "__main__":
    unittest.main()
