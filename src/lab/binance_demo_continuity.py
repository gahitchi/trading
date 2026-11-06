"""Compare two offline USD-M demo-account exports without promoting P&L.

Read-only, no HTTP/signing/credentials/order placement. Both JSON exports
remain untrusted, non-atomic observations; exact arithmetic and repeating
income IDs detect inconsistencies, not actual custody or completeness.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from lab.binance_demo_export import load_demo_export, normalize_demo_export

_NS_PER_MS = 1_000_000


def _income_history(bundle: dict) -> dict[tuple[str, int], tuple]:
    """The importer has already validated type, time, symbol and money."""
    return {
        (row["incomeType"], row["tranId"]): (
            row["time"],
            row.get("symbol", ""),
            row["asset"],
            Decimal(row["income"]),
        )
        for row in bundle["income"]
    }


def _difference(before: dict, after: dict) -> dict[str, str]:
    keys = set(before) | set(after)
    return {
        k: str(Decimal(after.get(k, "0")) - Decimal(before.get(k, "0")))
        for k in sorted(keys)
        if Decimal(after.get(k, "0")) != Decimal(before.get(k, "0"))
    }


def compare_demo_export_continuity(previous_file: Path, current_file: Path) -> dict:
    """Inspect a non-overlapping sequential pair from the *claimed* demo account.

    Income rows are considered only in the interior interval that cannot
    overlap either account collection window. Wallet differences may be due
    to fees, funding, transfers, trades or boundary-time movement: even an
    exact match is not a qualified account reconciliation.
    """
    previous, previous_sha = load_demo_export(previous_file)
    current, current_sha = load_demo_export(current_file)
    previous_view = normalize_demo_export(previous, source_sha256=previous_sha)
    current_view = normalize_demo_export(current, source_sha256=current_sha)

    if previous_sha == current_sha:
        raise ValueError("a second account observation cannot reuse the identical export")
    if previous_view["session_id"] != current_view["session_id"]:
        raise ValueError("demo export continuity requires the same session")
    if previous_view["account_alias_sha256"] != current_view["account_alias_sha256"]:
        raise ValueError("demo account alias changed between observations")
    if previous_view["environment"] != current_view["environment"]:
        raise ValueError("demo account environments differ")
    previous_end = previous_view["collected_end_ns"]
    current_start = current_view["collected_start_ns"]
    if previous_end >= current_start:
        raise ValueError("demo account collection intervals overlap or are reversed")

    # Binance income records expose millisecond timestamps. Do not
    # classify a transaction within either 30-second non-atomic GET
    # acquisition window as unambiguously between account snapshots.
    safe_first_ms = previous_end // _NS_PER_MS + 1
    safe_last_ms = (current_start - 1) // _NS_PER_MS
    if safe_first_ms > safe_last_ms:
        raise ValueError("no millisecond-separated interval between demo account observations")
    if (
        current["income_start_ms"] > safe_first_ms
        or current["income_end_ms"] < safe_last_ms
    ):
        raise ValueError("later income export does not claim coverage of the full between-observation interval")

    old_income = _income_history(previous)
    new_income = _income_history(current)

    # An overlapping query must not silently restate, void or omit a
    # previously seen transaction while claiming the same coverage.
    common_start = current["income_start_ms"]
    common_end = current["income_end_ms"]
    shared = 0
    for key, old in old_income.items():
        if common_start <= old[0] <= common_end:
            if key not in new_income:
                raise ValueError("previously observed income missing from overlapping new export")
            if old != new_income[key]:
                raise ValueError("previously observed income was restated by new export")
            shared += 1
        elif key in new_income and old != new_income[key]:
            raise ValueError("income transaction ID reused with conflicting source fields")

    claims: dict[tuple[str, str], Decimal] = {}
    asset_income: dict[str, Decimal] = {}
    interior_count = 0
    excluded_count = 0
    for (_kind, _txid), (ts, _symbol, asset, amount) in new_income.items():
        if safe_first_ms <= ts <= safe_last_ms:
            kind = _kind
            claims[(kind, asset)] = claims.get((kind, asset), Decimal(0)) + amount
            asset_income[asset] = asset_income.get(asset, Decimal(0)) + amount
            interior_count += 1
        else:
            excluded_count += 1

    wallet_delta = _difference(
        previous_view["wallet_balances"], current_view["wallet_balances"]
    )
    position_delta = _difference(
        previous_view["open_positions"], current_view["open_positions"]
    )
    assets = sorted(
        set(previous_view["wallet_balances"])
        | set(current_view["wallet_balances"])
        | set(asset_income)
    )
    unexplained_delta = {
        asset: str(
            Decimal(current_view["wallet_balances"].get(asset, "0"))
            - Decimal(previous_view["wallet_balances"].get(asset, "0"))
            - asset_income.get(asset, Decimal(0))
        )
        for asset in assets
    }
    previous_orders = previous_view["open_orders"]
    current_orders = current_view["open_orders"]
    common_order_keys = set(previous_orders) & set(current_orders)
    order_remaining_delta = {
        key: str(
            Decimal(current_orders[key]["remaining"])
            - Decimal(previous_orders[key]["remaining"])
        )
        for key in sorted(common_order_keys)
        if Decimal(current_orders[key]["remaining"]) != Decimal(previous_orders[key]["remaining"])
    }
    if any(
        previous_orders[k]["symbol"] != current_orders[k]["symbol"]
        for k in common_order_keys
    ):
        raise ValueError("demo client-order identity was reassigned to another symbol")

    residual_is_zero = all(Decimal(x) == 0 for x in unexplained_delta.values())
    return {
        "status": "two_demo_exports_compared_unverified",
        "environment": "binance-usdm-demo",
        "session_id": previous_view["session_id"],
        "previous_source_sha256": previous_sha,
        "current_source_sha256": current_sha,
        "account_alias_sha256": previous_view["account_alias_sha256"],
        "previous_collected_end_ns": previous_end,
        "current_collected_start_ns": current_start,
        "income_interior_first_ms": safe_first_ms,
        "income_interior_last_ms": safe_last_ms,
        "income_window_claims_cover_interior": True,
        "historical_income_rows_compared": shared,
        "interior_income_claim_count": interior_count,
        "excluded_income_claim_count": excluded_count,
        "interior_income_claims_by_type_asset": {
            f"{k[0]}:{k[1]}": str(v) for k, v in sorted(claims.items())
        },
        "wallet_balance_delta_by_asset": wallet_delta,
        "position_quantity_delta_by_symbol": position_delta,
        "orders_added": sorted(set(current_orders) - set(previous_orders)),
        "orders_removed": sorted(set(previous_orders) - set(current_orders)),
        "common_order_remaining_delta": order_remaining_delta,
        "wallet_delta_minus_interior_income_by_asset": unexplained_delta,
        "wallet_income_claims_arithmetically_equal": residual_is_zero,
        "provider_identity_authenticated": False,
        "income_coverage_independently_verified": False,
        "account_observations_atomic": False,
        "account_cash_settlement_reconciled": False,
        "nautilus_simulator_account_matched": False,
        "equity_reconciled": False,
        "funding_payment_verified": False,
        "margin_qualified": False,
        "risk_ready": False,
        "economic_posting_allowed": False,
    }
