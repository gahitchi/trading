"""Offline cross-snapshot Binance USD-M demo wallet/income consistency audit.

A successful numerical match is never proof of independent provider identity,
complete income history, authenticated wallet custody or valid net P&L.
The API responses were sampled separately and are not atomic.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from lab.binance_demo_export import _number, inspect_demo_export, load_demo_export, normalize_demo_export

_MS_NS = 1_000_000
_KNOWN_INCOME = {"FUNDING_FEE", "COMMISSION", "REALIZED_PNL", "TRANSFER"}


def _ceil_ms(ns: int) -> int:
    return (ns + _MS_NS - 1) // _MS_NS


def _income_index(rows: list[dict]) -> dict[tuple[str, int], dict]:
    return {(row["incomeType"], row["tranId"]): row for row in rows}


def compare_demo_cash_bundles(first: dict, second: dict, *, first_sha256: str, second_sha256: str) -> dict:
    """Require exact balance/income arithmetic with no boundary ambiguity.

    The later income window must replay the entire earlier window, cover
    both collection windows, and contain no events during either non-atomic
    sampling window. This is a stringent research diagnostic, not a
    guarantee of complete external funding or transfer coverage.
    """
    left = normalize_demo_export(first, source_sha256=first_sha256)
    right = normalize_demo_export(second, source_sha256=second_sha256)
    if first_sha256 == second_sha256:
        raise ValueError("two distinct demo exports are required")
    if left["account_alias_sha256"] != right["account_alias_sha256"]:
        raise ValueError("demo account aliases differ across exports")
    if left["collected_end_ns"] >= right["collected_start_ns"]:
        raise ValueError("demo export collection windows overlap or are reversed")
    first_start = first["collected_start_ns"] // _MS_NS
    first_end = _ceil_ms(first["collected_end_ns"])
    second_start = second["collected_start_ns"] // _MS_NS
    second_end = _ceil_ms(second["collected_end_ns"])
    if first_end >= second_start:
        raise ValueError("demo snapshots are too close for unambiguous transaction timestamps")
    if first["income_end_ms"] > first_start:
        raise ValueError("first demo income coverage extends into non-atomic collection")
    if second["income_start_ms"] > first["income_start_ms"]:
        raise ValueError("later demo income does not replay the full earlier covered history")
    if second["income_end_ms"] < second_end:
        raise ValueError("later demo income does not cover the second collection window")
    if second["income_end_ms"] - second["income_start_ms"] >= 86_400_000:
        raise ValueError("later demo income coverage exceeds bounded 24-hour interval")
    if left["unknown_income_type_count"] or right["unknown_income_type_count"]:
        raise ValueError("unrecognized demo income types prevent cash reconciliation")

    previous = _income_index(first["income"])
    subsequent = _income_index(second["income"])
    # The second query repeats the earlier query and cannot silently
    # revise, omit, or add old transaction IDs and amounts.
    historical_later = {
        key: row
        for key, row in subsequent.items()
        if first["income_start_ms"] <= row["time"] <= first["income_end_ms"]
    }
    if previous != historical_later:
        raise ValueError("historical demo income prefix differs between exports")
    changes: dict[tuple[str, str], Decimal] = {}
    for row in second["income"]:
        at = row["time"]
        if first_start <= at <= first_end or second_start <= at <= second_end:
            raise ValueError("demo income transaction falls inside non-atomic account observation")
        if first["income_end_ms"] < at <= first_end:
            raise ValueError("income between old coverage and first snapshot is ambiguous")
        if at <= first_end or at >= second_start:
            continue
        group = (row["incomeType"], row["asset"])
        changes[group] = changes.get(group, Decimal(0)) + _number(row["income"], "demo cash income")

    old_wallet = {k: _number(v, f"old {k} balance") for k, v in left["wallet_balances"].items()}
    new_wallet = {k: _number(v, f"new {k} balance") for k, v in right["wallet_balances"].items()}
    if old_wallet.keys() != new_wallet.keys():
        raise ValueError("demo wallet asset set changed; explicit asset transfer evidence is needed")
    if any(currency not in old_wallet for _kind, currency in changes):
        raise ValueError("income includes an asset outside the observed wallet set")
    deltas = {asset: new_wallet[asset] - old_wallet[asset] for asset in sorted(old_wallet)}
    income_sums = {asset: sum((v for (kind, cur), v in changes.items() if cur == asset), Decimal(0))
                   for asset in sorted(old_wallet)}
    if deltas != income_sums:
        raise ValueError("demo wallet balance delta differs from reported income/transfer cash movements")

    return {
        "status": "demo_wallet_income_delta_match_unverified",
        "environment": "binance-usdm-demo",
        "first_session": left["session_id"],
        "second_session": right["session_id"],
        "first_export_sha256": first_sha256,
        "second_export_sha256": second_sha256,
        "account_alias_sha256": left["account_alias_sha256"],
        "first_collected_end_ns": left["collected_end_ns"],
        "second_collected_start_ns": right["collected_start_ns"],
        "wallet_delta_by_currency": {k: str(v) for k, v in sorted(deltas.items())},
        "income_movements_by_type_currency": {
            f"{kind}:{currency}": str(value) for (kind, currency), value in sorted(changes.items())
        },
        "funding_fee_ledger_independently_authenticated": False,
        "source_authenticated": False,
        "income_coverage_verified": False,
        "cash_movements_complete": False,
        "snapshots_atomic": False,
        "nautilus_simulator_account_matched": False,
        "equity_reconciled": False,
        "margin_qualified": False,
        "risk_ready": False,
        "economic_posting_allowed": False,
    }


def compare_demo_cash_files(first_path: Path, second_path: Path) -> dict:
    """Read two already-existing local archives. Never create or edit them."""
    first, first_sha256 = load_demo_export(Path(first_path))
    second, second_sha256 = load_demo_export(Path(second_path))
    return compare_demo_cash_bundles(first, second, first_sha256=first_sha256, second_sha256=second_sha256)
