# Execution-store recovery fence and strict SQLite decoding (offline)

**Status:** candidate for independent G037 review, no broker connection.

**Parent branch:** `chatgpt/execution-store-cas-20261026` (G036, successful
GitHub CI at `9d39448ca02d39e8b0dbf9f90a541eac9a226882`).
**Child branch:** `chatgpt/execution-recovery-fence-20261026`.

## Problem

The prior `ExecutionStore` correctly preferred its SQLite snapshot over
the JSON projection when a snapshot existed, but failed open in three
recovery situations:

1. An **existing** SQLite database with no snapshot and no surviving
   `ledger.json` was treated as an uninitialized account. This could reset
   monetary state after snapshot deletion without a broker reconciliation.
2. If the authoritative snapshot disappeared while archived executions
   or terminal orders remained, an unbound legacy JSON projection could be
   imported as though it were the financial opening state.
3. SQLite page-level `PRAGMA quick_check` does not establish JSON semantics.
   `json.loads` accepts duplicate object keys, NaN and infinitely large
   exponent floats. Corrupt history's `coid` attribution index could disagree
   with its payload without the indexed getter detecting the contradiction.

## New invariants

- An existing SQLite DB with no `snapshot` requires a genuine *legacy
  import* candidate and an empty execution history. An existing DB with
  surviving history is **never** resumed by importing old unbound
  `ledger.json`, even if that projection supplies a plausible cash balance.
- A missing diagnostic JSON projection **is not** a reason to discard a
  valid authoritative SQLite snapshot. A missing SQLite database while a
  projection claims an authoritative store remains a hard blocker (G036).
- `decode()` refuses duplicate keys and nonfinite JSON before evaluating
  financial snapshots or indexed history. The existing semantic ledger
  validation and account/store binding checks still run.
- Indexed `applied` history reads verify the actual stored receipt/order
  attribution against the `coid` field. Explicit exports therefore fail
  on contradictory indexed attribution instead of silently exporting it.

## What this does NOT prove

- The local SQLite database is not a broker-authenticated execution source,
  and `quick_check` is not a cryptographic integrity mechanism.
- A completely empty SQLite database with no snapshot but a forged old
  `ledger.json` **still cannot be conclusively distinguished** from a
  genuinely interrupted first-time legacy import. This is an explicit
  remaining limitation until a durable, authenticated migration-intent
  record exists. Do not use this path for external broker qualification.
- Hashing a local payload is not proof a provider published it. Cash,
  commissions, short financing, late executions and splits still require
  independent source-bound reconciliation and B01–B08 acceptance.
- A missing snapshot, mismatched history or malformed financial JSON must
  produce operator-visible refusal. Never silently wipe or reconstruct
  a database, release reservations, or fabricate a fill.

## Portable test coverage

`tests/test_execution_recovery_fence.py` covers erased snapshots and
projections, a recreated legacy projection beside orphaned fills,
interrupted first import, preservation of a surviving authoritative
snapshot when JSON is missing, duplicate JSON keys, NaN/overflow floats,
and divergence of `history.coid` from decoded execution attribution.

Mandatory CI: Ruff check+format, Python 3.13 and 3.14 fast and real-clock
harness, offline example on real prices, package, Docker and collaboration.
No claim of a release pass until those checks and Claude's G037 review.
