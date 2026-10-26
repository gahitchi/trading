# Execution state: monotonic SQLite writer fence (candidate)

**Scope:** internal development, offline SQLite execution snapshots, no broker
credentials and no external order permission.

## Finding

Earlier `ExecutionStore.save()` unconditionally replaced the current
`snapshot` row, and historical execution IDs used an
`ON CONFLICT DO UPDATE` upsert. Two independent writers that loaded the same
state could therefore replace each other's positions, cash, pending orders
and replay checkpoint. A reused applied trade ID could also have its order ID
or timestamp rewritten, undermining immutable deduplication.

## Changes

- SQLite schema migrates **v1 to v2** with a `snapshot.generation` column,
  preserving existing ledger/history records. New databases start at v2.
- Each store instance pins both the snapshot bytes and generation at opening.
  Inside `BEGIN IMMEDIATE`, `save()` checks they still equal the database's
  current head. Any competing writer causes an explicit **stale-writer
  exception** and a transaction rollback, before committing any history.
- Every committed snapshot increments the generation, including an identical
  resave. This prevents an ABA case where another writer brings the bytes
  back to the original value.
- Applied trade IDs are insert-only; identical redelivery is idempotent.
  Conflicting values (receipt timestamp or client order ID) are refused within
  the same in-memory batch and checked atomically against committed history.
  Terminal/disposed order states remain intentionally updateable for late
  broker evidence and reactivation; they are not incorrectly classified as
  immutable trades.
- On a SQLite commit followed by failure to write the diagnostic JSON
  projection, the local generation is advanced **before** that projection:
  authoritative accounting remains on SQLite. The next save by the same
  instance uses the correct committed head.

## Fail-closed semantics

A stale writer **must not automatically retry** its intended snapshot. It
has potentially stale cash, fills, and reservations. A fresh operator-reviewed
session must reconstruct authoritative ledger state and reconcile actual
broker reports before submitting anything. Never copy `ledger.json` over
`ledger.sqlite3`.

An old binary using schema v1 rejects schema v2 rather than silently
downgrading. Back up the full runtime before applying this migration. The
upgrade is an internal release-candidate change; paper-broker acceptance
remains outstanding.

This is a *single SQLite database* compare-and-swap invariant, not a
distributed lock or source authentication. It does not establish broker
cash parity, completeness of order history, exchange sequence authenticity,
or capital-moving authorization.

## Regression coverage

`tests/test_execution_store_cas.py`:
- two startup writers racing from a blank ledger;
- preexisting snapshot race and a true concurrent two-writer race;
- identical-byte ABA after a resave;
- in-memory and committed trade-ID attribution conflicts;
- atomic rollback of unrelated terminal history after a conflicting trade;
- v1 schema migration preserving cash/history;
- failure between SQLite commit and diagnostic JSON projection;
- unknown schema version refusal.

Existing `tests/test_execution_store.py` validates legacy migration,
bounded history, SQLite capacity exhaustion, crash/kill recovery,
and database/projection mismatch.

Review with `python tools/collab.py check`, Ruff lint/format and the
hosted Python 3.13/3.14 test/harness tiers before integrating. Never
relax paper-only or release gates as part of this work.
