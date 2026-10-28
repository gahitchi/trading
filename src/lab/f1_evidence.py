"""F1.6 local reproducibility and evidence receipt; NEVER external attestation.

This module reads one SQLite transaction and reports evidence without writing
to the economic ledger or connecting to a broker. Source/content integrity is
not origin authentication; an unkeyed digest is not an independently signed
root. No F0/F1 qualification or trading authority is implied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from lab.account_projection import compare_projection
from lab.economic_store import EconomicEventStore, EconomicStoreError, MAX_ORDER_OBSERVATIONS
from lab.engine_events import BrokerFillFact, EngineEconomicBook, _evidence_digest, _identity
from lab.order_lifecycle import _replay, parse_broker_order_event
from lab.order_observation import parse_external_order_statement, reconcile_external_orders

GIT_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _canonical(record: object) -> bytes:
    return json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


@dataclass(frozen=True, slots=True)
class F1LocalEvidence:
    """Hash-complete local diagnostic, not a qualified or broker-signed audit."""

    manifest: dict
    sha256: str

    def as_json(self) -> bytes:
        return _canonical({"manifest": self.manifest, "manifest_sha256": self.sha256})


def _verify_archived_observations(db: sqlite3.Connection, book: EngineEconomicBook) -> tuple[int, int]:
    """Verify all historic reports against the original economic prefix."""
    heads = ["0" * 64]
    for fact in book.events:
        heads.append(_evidence_digest(heads[-1], book.account_id, fact))
    rows = db.execute(
        "SELECT seq,source_ref,source_namespace,as_of_ns,event_count,evidence_head,"
        "source_sha256,source_bytes FROM external_order_observations ORDER BY seq"
    ).fetchall()
    if len(rows) > MAX_ORDER_OBSERVATIONS:
        raise EconomicStoreError("external order evidence archive exceeds its maximum size")
    checked_fills = 0
    prefixes: dict[int, EngineEconomicBook] = {0: EngineEconomicBook(book.account_id)}
    for count, row in enumerate(rows, 1):
        seq, source_ref, namespace, as_of, event_count, head, digest, raw = row
        if (
            type(seq) is not int
            or seq != count
            or type(event_count) is not int
            or not 0 <= event_count <= len(book.events)
            or type(head) is not str
            or head != heads[event_count]
            or type(raw) is not bytes
        ):
            raise EconomicStoreError("archived order statement sequence/head corrupted")
        report = parse_external_order_statement(
            raw, source_namespace=namespace, expected_account_id=book.account_id
        )
        if (
            report.schema_version != 2
            or report.source_ref != source_ref
            or report.as_of_ns != as_of
            or report.source_sha256 != digest
        ):
            raise EconomicStoreError("archived original order source contradicts custody metadata")
        if event_count not in prefixes:
            prefix = EngineEconomicBook(book.account_id)
            for fact in book.events[:event_count]:
                prefix = prefix.append(fact)
            prefixes[event_count] = prefix
        snapshot = prefixes[event_count]
        if snapshot.evidence_head != head:
            raise EconomicStoreError("archived order prefix checksum changed")
        parity = reconcile_external_orders(snapshot, report)
        if not parity.matches or not parity.receipt_scope_checked:
            raise EconomicStoreError("archived broker order report contradicts original source facts")
        checked_fills += parity.broker_fills_checked
    return len(rows), checked_fills


def build_f1_local_evidence(store: EconomicEventStore, *, declared_commit_sha: str) -> F1LocalEvidence:
    """Produce one deterministic manifest from a single, read-only DB snapshot.

    The caller-declared commit is intentionally NOT verified against a Git
    checkout. Verification of code/build identity must come from F0 evidence
    and an independent reviewer. Nothing here can prove an issuer's identity.
    """
    if type(store) is not EconomicEventStore or type(declared_commit_sha) is not str:
        raise EconomicStoreError("explicit local journal and declared exact commit required")
    if not GIT_COMMIT_SHA.fullmatch(declared_commit_sha):
        raise EconomicStoreError("declared code revision must be a full 40-character Git SHA")
    with closing(store._connect()) as db:
        db.execute("BEGIN")
        book = store._read_locked(db)
        parity = compare_projection(book)
        if not parity.matches:
            raise EconomicStoreError("independent rational projection disagrees with double-entry ledger")
        lifecycle = _replay(db, book)
        orders = db.execute(
            "SELECT source_namespace,source_bytes FROM broker_order_events ORDER BY seq"
        ).fetchall()
        tracked_fill_source_refs = set()
        for namespace, raw_order in orders:
            order = parse_broker_order_event(
                raw_order, source_namespace=namespace, expected_account_id=book.account_id
            )
            if order.kind == "FILL":
                tracked_fill_source_refs.add(f"{namespace}:{order.fill_record_id}")
        all_fill_refs = {
            fact.source_ref for fact in book.events if type(fact) is BrokerFillFact
        }
        if not tracked_fill_source_refs <= all_fill_refs:
            raise EconomicStoreError("order custody claims broker fills absent from economic history")
        complete_lifecycle_coverage = all_fill_refs == tracked_fill_source_refs
        archives, archived_fills = _verify_archived_observations(db, book)
        event_roots = []
        for index, fact in enumerate(book.events, 1):
            event_roots.append(
                {
                    "seq": index,
                    "event_id": fact.event_id,
                    "source_ref": fact.source_ref,
                    "source_sha256": getattr(fact, "source_sha256", None),
                    "economic_type": type(fact).__name__,
                }
            )
        receipt_sha256 = hashlib.sha256(_canonical(event_roots)).hexdigest()
        manifest = {
            "schema_version": 1,
            "purpose": "F1_LOCAL_UNQUALIFIED_REPLAY_EVIDENCE",
            "account_id": _identity(store.account_id, "F1 account"),
            "declared_commit_sha": declared_commit_sha,
            "event_count": len(book.events),
            "economic_source_head": book.evidence_head,
            "economic_postings_head": book.ledger.journal[-1].sha256 if book.events else "0" * 64,
            "source_inventory_sha256": receipt_sha256,
            "source_inventory_count": len(event_roots),
            "rational_projection_matches": True,
            "broker_fill_count": len(all_fill_refs),
            "order_fill_receipt_count": len(tracked_fill_source_refs),
            "complete_local_fill_lifecycle_coverage": complete_lifecycle_coverage,
            "order_event_count": lifecycle.sequence,
            "order_evidence_head": lifecycle.evidence_head,
            "unresolved_order_ids": list(lifecycle.unresolved_order_ids),
            "archived_order_observations_replayed": archives,
            "archived_broker_fill_receipts_replayed": archived_fills,
            "broker_source_authenticated": False,
            "evidence_root_independently_anchored": False,
            "arch_operator_verified": False,
            "f0_qualified": False,
            "f1_qualified": False,
            "release_authorized": False,
            "execution_authorized": False,
        }
        db.execute("COMMIT")
    return F1LocalEvidence(manifest, hashlib.sha256(_canonical(manifest)).hexdigest())


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only F1 local custody manifest (no external attestation)")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--commit", required=True, help="Full declared commit SHA, not cryptographically verified here")
    args = parser.parse_args(argv)
    try:
        result = build_f1_local_evidence(
            EconomicEventStore(args.journal, args.account, readonly=True),
            declared_commit_sha=args.commit,
        )
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        print(f"F1 local evidence refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    sys.stdout.buffer.write(result.as_json() + b"\n")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
