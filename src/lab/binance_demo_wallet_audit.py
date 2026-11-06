"""Offline diagnostic of wallet balance changes between two Binance demo exports.

Uses two independently timestamped *unverified* user-supplied snapshots.
Only exact signed income rows in an explicitly covered gap are compared.
Even a zero residual cannot authenticate API provenance, prove all
payments were reported, or authorize equity, trading or reinvestment.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from pathlib import Path

from lab.binance_demo_export import load_demo_export, normalize_demo_export

_NS_PER_MS = 1_000_000


def compare_demo_wallet_exports(before: Path, after: Path) -> dict:
    """Diagnose wallet delta vs claimed income, rejecting coverage ambiguity.

    Requests and account snapshots are NOT atomic. Income records dated
    during either collection interval cannot be safely attributed and are
    rejected, rather than guessed into the covered period.
    """
    first, first_sha = load_demo_export(before)
    second, second_sha = load_demo_export(after)
    start = normalize_demo_export(first, source_sha256=first_sha)
    end = normalize_demo_export(second, source_sha256=second_sha)
    if first_sha == second_sha:
        raise ValueError("demo export is repeated, not a second observation")
    if first["session_id"] != second["session_id"]:
        raise ValueError("demo exports belong to different sessions")
    if start["account_alias_sha256"] != end["account_alias_sha256"]:
        raise ValueError("demo exports belong to different account aliases")
    if first["collected_end_ns"] >= second["collected_start_ns"]:
        raise ValueError("demo export collection intervals overlap or are reversed")

    lower = first["collected_end_ns"]
    upper = second["collected_start_ns"]
    if second["income_start_ms"] * _NS_PER_MS > lower:
        raise ValueError("second income window misses the start of the wallet interval")
    if second["income_end_ms"] * _NS_PER_MS < upper:
        raise ValueError("second income window does not cover the end of the wallet interval")

    first_wallet = start["wallet_balances"]
    second_wallet = end["wallet_balances"]
    if set(first_wallet) != set(second_wallet):
        raise ValueError("demo currency inventory changed; cannot invent missing wallet balances")

    # Cross-export overlap is useful for detecting restated income rows,
    # although matching records are not provider authentication.
    historical = {
        (entry["incomeType"], entry["tranId"]): entry
        for entry in first["income"]
    }
    with localcontext() as context:
        context.prec = 80
        totals = {asset: Decimal(0) for asset in first_wallet}
        source_ids = []
        unknown_types = set()
        for entry in second["income"]:
            event_ns = entry["time"] * _NS_PER_MS
            if (
                first["collected_start_ns"] <= event_ns <= first["collected_end_ns"]
                or second["collected_start_ns"] <= event_ns <= second["collected_end_ns"]
            ):
                raise ValueError("income event falls inside a non-atomic account collection interval")
            key = (entry["incomeType"], entry["tranId"])
            if key in historical and entry != historical[key]:
                raise ValueError("previously observed income transaction was restated")
            if event_ns <= lower:
                continue
            if event_ns > upper:
                # Should have been rejected as a collection-interval
                # event, except for genuinely future/uncovered rows.
                raise ValueError("income item extends beyond the current account anchor")
            asset = entry["asset"]
            if asset not in totals:
                raise ValueError("demo income currency has no matching wallet in both exports")
            amount = Decimal(entry["income"])
            totals[asset] += amount
            source_ids.append(f"{entry['incomeType']}:{entry['tranId']}")
            if entry["incomeType"] not in {"FUNDING_FEE", "COMMISSION", "REALIZED_PNL", "TRANSFER"}:
                unknown_types.add(entry["incomeType"])
        deltas = {
            asset: Decimal(second_wallet[asset]) - Decimal(first_wallet[asset])
            for asset in first_wallet
        }
        unexplained = {asset: deltas[asset] - totals[asset] for asset in deltas}
        exact = all(amount == 0 for amount in unexplained.values())
        str_value = lambda x: format(x, "f")
        return {
            "status": "wallet_delta_consistent_unverified" if exact else "wallet_delta_unexplained",
            "session_id": first["session_id"],
            "account_alias_sha256": start["account_alias_sha256"],
            "previous_source_sha256": first_sha,
            "current_source_sha256": second_sha,
            "period_start_ns": lower,
            "period_end_ns": upper,
            "currency_wallet_delta": {k: str_value(v) for k, v in sorted(deltas.items())},
            "currency_income_claims": {k: str_value(v) for k, v in sorted(totals.items())},
            "currency_unexplained_residual": {k: str_value(v) for k, v in sorted(unexplained.items())},
            "income_claim_count": len(source_ids),
            "unknown_income_types": sorted(unknown_types),
            "wallet_delta_matches_claimed_income": exact,
            "provider_identity_authenticated": False,
            "income_coverage_independently_verified": False,
            "account_snapshot_atomic": False,
            "actual_cash_settlements_verified": False,
            "equity_reconciled": False,
            "margin_qualified": False,
            "risk_ready": False,
            "economic_posting_allowed": False,
        }
