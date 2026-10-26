"""Provider-neutral trial review tests; no brokerage credentials or network."""

from __future__ import annotations

import unittest
from dataclasses import replace

from lab.broker_trial import (
    ProbeEvidence,
    ProbeKind,
    ProbeStatus,
    TrialAsset,
    TrialContract,
    TrialScope,
    assess_trial_claims,
)

HASH = "a" * 64
NOW = 100_000
AGE = 1_000


def scoped(asset: TrialAsset = TrialAsset.EQUITY, kind: TrialContract = TrialContract.SHARE) -> TrialScope:
    return TrialScope(
        venue="TEST_VENUE",
        account_scope="PAPER_ALIAS_ONLY",
        environment="paper",
        instrument_id="SIM:A",
        asset_class=asset,
        contract_kind=kind,
        action_version="a1",
        epoch="session1",
    )


def evidence(scope: TrialScope, *, captured: int = NOW, status: ProbeStatus = ProbeStatus.OBSERVED):
    return tuple(
        ProbeEvidence(scope, kind, status, captured, HASH) for kind in sorted(scope.required(), key=lambda k: k.value)
    )


class BrokerTrialTests(unittest.TestCase):
    def test_all_claims_observed_is_still_not_broker_authentication(self):
        scope = scoped()
        report = assess_trial_claims(scope, evidence(scope), now_ns=NOW, maximum_age_ns=AGE)
        self.assertTrue(report.claimed_coverage_complete)
        self.assertFalse(report.authenticated_broker)
        self.assertFalse(report.release_reservations)
        self.assertFalse(report.authorize_orders)

    def test_missing_order_history_is_a_hard_gap(self):
        scope = scoped()
        records = tuple(x for x in evidence(scope) if x.kind is not ProbeKind.HISTORICAL_ORDERS)
        report = assess_trial_claims(scope, records, now_ns=NOW, maximum_age_ns=AGE)
        self.assertIn(ProbeKind.HISTORICAL_ORDERS, report.missing)
        self.assertFalse(report.claimed_coverage_complete)

    def test_explicitly_missing_or_conflicted_evidence_is_never_silenced(self):
        scope = scoped()
        records = list(evidence(scope))
        records[0] = replace(records[0], status=ProbeStatus.MISSING)
        records[1] = replace(records[1], status=ProbeStatus.CONFLICT)
        report = assess_trial_claims(scope, tuple(records), now_ns=NOW, maximum_age_ns=AGE)
        self.assertIn(records[0].kind, report.missing)
        self.assertIn(records[1].kind, report.conflicting)
        self.assertFalse(report.claimed_coverage_complete)

    def test_stale_and_future_dated_claims_are_not_current(self):
        scope = scoped()
        old = replace(evidence(scope)[0], captured_ns=NOW - AGE - 1)
        future = replace(evidence(scope)[1], captured_ns=NOW + 1)
        records = (old, future, *evidence(scope)[2:])
        result = assess_trial_claims(scope, records, now_ns=NOW, maximum_age_ns=AGE)
        self.assertIn(old.kind, result.stale)
        self.assertIn(future.kind, result.future_dated)
        self.assertFalse(result.claimed_coverage_complete)

    def test_duplicate_and_cross_account_probes_are_detected(self):
        scope = scoped()
        first = evidence(scope)[0]
        wrong = replace(evidence(scope)[1], scope=replace(scope, account_scope="FOREIGN_SCOPE"))
        records = (first, first, wrong, *evidence(scope)[2:])
        outcome = assess_trial_claims(scope, records, now_ns=NOW, maximum_age_ns=AGE)
        self.assertIn(first.kind, outcome.duplicate)
        self.assertIn(wrong.kind, outcome.cross_scope)
        self.assertFalse(outcome.claimed_coverage_complete)

    def test_reconnect_epoch_source_switch_requires_new_probe_scope(self):
        scope = scoped()
        record = replace(evidence(scope)[0], scope=replace(scope, epoch="session2"))
        other = (record, *evidence(scope)[1:])
        out = assess_trial_claims(scope, other, now_ns=NOW, maximum_age_ns=AGE)
        self.assertIn(record.kind, out.cross_scope)

    def test_equity_forex_crypto_derivatives_require_different_proof(self):
        stock = scoped()
        fx = scoped(TrialAsset.FOREX, TrialContract.FOREX_CASH)
        spot = scoped(TrialAsset.CRYPTO, TrialContract.SPOT)
        deriv = scoped(TrialAsset.CRYPTO, TrialContract.LINEAR_DERIVATIVE)
        self.assertIn(ProbeKind.EQUITY_CORPORATE_ACTIONS, stock.required())
        self.assertNotIn(ProbeKind.FX_ROLLOVER, stock.required())
        self.assertIn(ProbeKind.FX_ROLLOVER, fx.required())
        self.assertIn(ProbeKind.CRYPTO_BOOK_SEQUENCE, spot.required())
        self.assertNotIn(ProbeKind.DERIVATIVE_FUNDING, spot.required())
        self.assertIn(ProbeKind.DERIVATIVE_FUNDING, deriv.required())
        self.assertIn(ProbeKind.DERIVATIVE_LIQUIDATION, deriv.required())
        for item in (stock, fx, spot, deriv):
            self.assertIn(ProbeKind.INCOMPLETE_HISTORY, item.required())
            self.assertIn(ProbeKind.CANCEL_FILL_RACE, item.required())

    def test_strict_paper_only_and_instrument_classification(self):
        with self.assertRaisesRegex(ValueError, "only paper"):
            replace(scoped(), environment="live")
        with self.assertRaisesRegex(ValueError, "only paper"):
            replace(scoped(), environment="offline_fixture")
        with self.assertRaisesRegex(ValueError, "contract kind mismatch"):
            replace(scoped(), contract_kind=TrialContract.SPOT)
        with self.assertRaises(ValueError):
            replace(scoped(), account_scope="PAPER ALIAS")

    def test_evidence_requires_valid_immutable_types_and_digest(self):
        scope = scoped()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            ProbeEvidence(scope, ProbeKind.EXECUTIONS, ProbeStatus.OBSERVED, NOW, "not-a-sha")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            ProbeEvidence(scope, ProbeKind.EXECUTIONS, ProbeStatus.OBSERVED, NOW, "A" * 64)
        with self.assertRaisesRegex(ValueError, "capture"):
            ProbeEvidence(scope, ProbeKind.EXECUTIONS, ProbeStatus.OBSERVED, True, HASH)
        with self.assertRaisesRegex(ValueError, "unknown"):
            ProbeEvidence(scope, ProbeKind.EXECUTIONS, "observed", NOW, HASH)
        with self.assertRaisesRegex(ValueError, "immutable"):
            assess_trial_claims(scope, list(evidence(scope)), now_ns=NOW, maximum_age_ns=AGE)

    def test_time_policy_rejects_zero_bool_negative(self):
        scope = scoped()
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess_trial_claims(scope, evidence(scope), now_ns=NOW, maximum_age_ns=value)

    def test_report_cannot_be_instantiated_with_order_authority(self):
        scope = scoped()
        report = assess_trial_claims(scope, evidence(scope), now_ns=NOW, maximum_age_ns=AGE)
        with self.assertRaises(TypeError):
            type(report)(
                venue=scope.venue,
                instrument_id=scope.instrument_id,
                missing=(),
                conflicting=(),
                stale=(),
                future_dated=(),
                duplicate=(),
                cross_scope=(),
                authorize_orders=True,
            )
        with self.assertRaises((AttributeError, TypeError)):
            report.authorize_orders = True


if __name__ == "__main__":
    unittest.main()
