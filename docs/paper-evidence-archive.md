# Paper execution evidence archive (research-only)

**Implemented 2026-11-05 on the maintenance branch.**
Module: src/lab/paper_evidence.py; regression suite: tests/test_paper_evidence.py.

This is a **portable source-evidence boundary**, not a broker adapter,
execution engine, exchange statement, authenticated order feed or proof
of external fills. All event and price observations are supplied by a
separate supervised sandbox/replay producer. An operator-supplied source
label or a SHA-256 checksum does **not** authenticate those observations.
There is no exchange API key usage or order placement.

## Data contract

Each paper_report envelope carries:

- A specified origin, either nautilus-sandbox or offline-replay.
- A UTC capture time not before the simulated event time.
- A normalized SimulatedFill, PositionMark, or CashAdjustment, with the
  full source ID/time and independently supplied position mark quotes
  required by lab.position_reconcile.
- The original paper producer's JSON receipt, including the matching
  source_id. Other fields are preserved for later auditing, not
  interpreted as independently authenticated evidence.

No raw receipt may exceed 1 MB. Source event identities must be unique;
all economic inputs must form a chronological, causal stream. The
underlying reconciler rejects stale/future marks, missing open-position
coverage, invalid amounts, duplicates and missing UTC day closes.

The archive stores a canonical JSONL header and append-only records
with predecessor SHA-256 linkage. A separately fsynced, sealed
.head.json commits the accepted suffix; a writer takes an exclusive
file lock. Torn records, changed bytes, missing head, changed policy,
wrong origin and truncated committed suffix **fail closed**. Manual
recovery after interruption requires an audited policy: do not delete
or silently regenerate an inconsistent checkpoint. The integrity chain
cannot defend against deliberate rewriting of *both* the journal and
checkpoint by an adversary, nor device loss without backup.

## Call sequence

1. The paper execution/market adapter obtains a complete original
   simulated execution or mark receipt and normalizes it without
   guessing missing fills, quote provenance or funding.
2. Wrap it using paper_report(event, origin="nautilus-sandbox",
   captured_at=UTC_CAPTURE_TIME, raw_receipt=ORIGINAL_RECEIPT).
3. Call PaperEvidenceArchive.append(report) **before** posting the
   economic effect.
4. Call archive.reconcile(profit_journal). It verifies the **entire**
   recorded source against the full economic journal prefix. An
   archived-but-unposted suffix from a crash can be posted exactly once
   on restart; evidence disappearance or economic divergence stops.
5. Supply explicit position marks at midnight UTC for all open
   exposures, even on zero-trade days.

This is a conservative, offline validation path. Each append rereads and
recompiles the complete source, and each reconciliation rereads the full
journal. Neither operation belongs in a real-time order hot loop.

## Outstanding qualification

- The raw NautilusTrader sandbox lifecycle event recorder is now
  implemented (docs/sandbox-execution-capture.md), including
  simulated fill/order IDs and timestamps. **Still missing:** a
  complete externally corroborated account report, independently timed
  market-mark observations, settlement/fee-currency verification and
  a validated conversion into this economic source archive.
- Collect durable, externally auditable producer receipts; establish
  independent provenance or authenticated source checks rather than
  trusting a receipt's self-declared origin.
- Reconcile full paper account inventories, wallet balances and
  outstanding orders, not only linear-perpetual realized/unrealized P&L.
- Model and test venue margin/liquidation, risk-manager sizing/kill gates,
  FX conversion, and multi-account cash segmentation.
- Stress concurrent writers, interrupted captures, cross-journal
  failures, deployment latency and supervised session restarts.
- Do not qualify a research strategy, release a protected holdout,
  configure live exchange keys or send real orders based on this module.
