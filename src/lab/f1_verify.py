"""Offline F1 research MVP entry point: verify owned ledger vs external statement.

Usage:
    python -m lab.f1_verify --journal ./economic.sqlite3 --account SIM-001 \
      --statement ./account-report.json --source broker-export

This command is read-only. It does NOT open a broker session, submit orders,
authenticate a statement issuer, qualify release or authorize execution.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from lab.account_observation import (
    MAX_STATEMENT_BYTES,
    parse_external_account_statement,
    reconcile_external_account,
)
from lab.economic_ledger import EconomicLedgerError
from lab.economic_store import EconomicEventStore, EconomicStoreError
from lab.engine_events import BrokerFillFact, EngineFactError
from lab.order_observation import (
    MAX_ORDER_REPORT_BYTES,
    parse_external_order_statement,
    reconcile_external_orders,
)
from lab.order_lifecycle import OrderLifecycleStore, reconcile_order_lifecycle


def _decimal_text(value) -> str:
    """Canonical JSON decimal text, independent of input's trailing zero scale."""
    return "0" if value == 0 else format(value, "f")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only exact F1 economic reconciliation (no trading)")
    parser.add_argument("--journal", type=Path, required=True, help="Existing SQLite economic journal")
    parser.add_argument("--account", required=True, help="Explicit account identity")
    parser.add_argument("--statement", type=Path, required=True, help="Existing independent account report JSON")
    parser.add_argument("--source", required=True, help="Provenance namespace of the external report")
    parser.add_argument(
        "--orders",
        type=Path,
        help="Independent complete order report, required if broker fills exist in the journal",
    )
    parser.add_argument(
        "--require-order-lifecycle",
        action="store_true",
        help="Also require original, replayed order ACK/fill/cancel state against the complete report",
    )
    args = parser.parse_args(argv)
    try:
        with args.statement.open("rb") as handle:
            raw = handle.read(MAX_STATEMENT_BYTES + 1)
        statement = parse_external_account_statement(
            raw, source_namespace=args.source, expected_account_id=args.account
        )
        store = EconomicEventStore(args.journal, args.account, readonly=True)
        book = store.load()
        parity = reconcile_external_account(book, statement)
        order_parity = None
        lifecycle_parity = None
        if args.require_order_lifecycle and args.orders is None:
            raise EngineFactError("order lifecycle parity requires a complete order report")
        if args.orders is not None:
            with args.orders.open("rb") as handle:
                order_raw = handle.read(MAX_ORDER_REPORT_BYTES + 1)
            order_report = parse_external_order_statement(
                order_raw, source_namespace=args.source, expected_account_id=args.account
            )
            if order_report.as_of_ns != statement.as_of_ns:
                raise EngineFactError("account and order observations are from different snapshot times")
            if any(type(fact) is BrokerFillFact for fact in book.events) and order_report.schema_version != 2:
                raise EngineFactError("broker-fill parity requires a v2 complete execution receipt inventory")
            order_parity = reconcile_external_orders(book, order_report)
            if args.require_order_lifecycle:
                lifecycle_parity = reconcile_order_lifecycle(OrderLifecycleStore(store), order_report)
                if lifecycle_parity.economic_head != book.evidence_head:
                    raise EngineFactError("economic ledger advanced during read-only lifecycle verification")
        elif any(type(fact) is BrokerFillFact for fact in book.events):
            raise EngineFactError("broker fills require a separate complete order report")
    except (
        OSError,
        ValueError,
        EconomicLedgerError,
        EconomicStoreError,
        EngineFactError,
        sqlite3.DatabaseError,
    ) as exc:
        print(f"F1 verification refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    exact_match = (
        parity.matches
        and (order_parity is None or order_parity.matches)
        and (lifecycle_parity is None or lifecycle_parity.matches)
    )
    result = {
        "schema_version": 1,
        "status": "MATCH" if exact_match else "MISMATCH",
        "account_id": parity.account_id,
        "statement_source_ref": parity.statement_source_ref,
        "statement_sha256": parity.statement_sha256,
        "ledger_evidence_head": parity.journal_evidence_head,
        "events_replayed": len(book.events),
        "cash_delta_usd": _decimal_text(parity.cash_delta),
        "position_deltas": [
            {"symbol": symbol, "quantity_delta": _decimal_text(delta)} for symbol, delta in parity.position_deltas
        ],
        "independent_projection_matches": parity.internal_projection_matches,
        "order_parity_checked": order_parity is not None,
        "order_statement_sha256": order_parity.statement_sha256 if order_parity else None,
        "broker_fills_checked": order_parity.broker_fills_checked if order_parity else None,
        "order_receipt_scope_checked": order_parity.receipt_scope_checked if order_parity else False,
        "order_receipt_identities_checked": order_parity.receipt_identities_checked if order_parity else 0,
        "order_deltas": (
            [
                {
                    "broker_order_id": delta.broker_order_id,
                    "reported": _decimal_text(delta.report_quantity),
                    "recorded": _decimal_text(delta.custody_quantity),
                    "quantity_delta": _decimal_text(delta.delta),
                }
                for delta in order_parity.order_deltas
            ]
            if order_parity
            else None
        ),
        "order_lifecycle_checked": lifecycle_parity is not None,
        "order_lifecycle_matches": lifecycle_parity.matches if lifecycle_parity else None,
        "order_lifecycle_evidence_head": lifecycle_parity.order_head if lifecycle_parity else None,
        "order_lifecycle_deltas": (
            [
                {
                    "broker_order_id": item.broker_order_id,
                    "recorded_status": item.recorded_status,
                    "reported_status": item.reported_status,
                    "requested_delta": _decimal_text(item.requested_delta),
                    "executed_delta": _decimal_text(item.executed_delta),
                    "remaining_delta": _decimal_text(item.remaining_delta),
                }
                for item in lifecycle_parity.order_deltas
            ]
            if lifecycle_parity
            else None
        ),
        "broker_source_authenticated": False,
        "release_authorized": False,
        "execution_authorized": False,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if exact_match else 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
