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
from lab.engine_events import EngineFactError


def _decimal_text(value) -> str:
    """Canonical JSON decimal text, independent of input's trailing zero scale."""
    return "0" if value == 0 else format(value, "f")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only exact F1 economic reconciliation (no trading)")
    parser.add_argument("--journal", type=Path, required=True, help="Existing SQLite economic journal")
    parser.add_argument("--account", required=True, help="Explicit account identity")
    parser.add_argument("--statement", type=Path, required=True, help="Existing independent account report JSON")
    parser.add_argument("--source", required=True, help="Provenance namespace of the external report")
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
    except (OSError, ValueError, EconomicLedgerError, EconomicStoreError, EngineFactError, sqlite3.DatabaseError) as exc:
        print(f"F1 verification refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    result = {
        "schema_version": 1,
        "status": "MATCH" if parity.matches else "MISMATCH",
        "account_id": parity.account_id,
        "statement_source_ref": parity.statement_source_ref,
        "statement_sha256": parity.statement_sha256,
        "ledger_evidence_head": parity.journal_evidence_head,
        "events_replayed": len(book.events),
        "cash_delta_usd": _decimal_text(parity.cash_delta),
        "position_deltas": [
            {"symbol": symbol, "quantity_delta": _decimal_text(delta)}
            for symbol, delta in parity.position_deltas
        ],
        "independent_projection_matches": parity.internal_projection_matches,
        "broker_source_authenticated": False,
        "release_authorized": False,
        "execution_authorized": False,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if parity.matches else 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
