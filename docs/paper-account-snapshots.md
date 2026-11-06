# Paper account snapshot validation — portable, unqualified

Implementation: `lab.paper_account_snapshot`, `lab.sandbox_capture.PaperOrderEventCapture.evidence_prefix`.
Regressions: `tests/test_paper_account_snapshot.py`. No broker imports, exchange credentials or
order submission. Only the `chatgpt/codex-maintenance` branch is changed.

## Trust boundary

A snapshot here is supplied by an **external caller** and carries
`source_kind="sandbox-cache"`; the source name, signed booleans and hash
**do not authenticate** the provider. Without independently archived
NautilusTrader cache, wallet and order-report evidence, this is a
validation *interface*, not qualified broker/account reconciliation.

`PaperAccountSnapshot` requires:

- stable `snapshot_id`, `source_id`, `account_id`, and `captured_ns`;
- exact `capture_count` and `capture_sha256` of an earlier verified
  `PaperOrderEventCapture.evidence_prefix(count)`;
- explicit true `started_flat`, `exclusive_scope` and
  `complete_snapshot` assertions (not verifiable from the raw events);
- complete `positions` mapping, signed Decimal strings;
- `open_orders` mapping of client-order ID to remaining quantity;
- `balances` by currency, with Decimal-string **cash** `total`,
  `available` and `locked` (total = available + locked). These are not
  margin-equity quantities or proof of custody.

The validator binds the snapshot to a verified, immutable historical
receipt prefix, checks order receipts were observed before the
snapshot, rejects any omitted earlier-received receipts, forbids
duplicate trade IDs, fill voids and account-identity drift, and checks
the signed fill delta from a separately attested flat account start
against the snapshot positions.

It checks every **reported** open order has a captured client-order ID
and is not last seen in a canceled/rejected/expired terminal state.
It **cannot** prove that all outstanding orders are present, or that
the reported remaining order quantities are accurate: the upstream
capture event schema lacks authoritative remaining-quantity and
total-order accounting. A completely filled order cannot reliably be
distinguished from a partially filled order merely from `OrderFilled`.

Cash balance conservation is arithmetic only. The validator does not
reconcile profit, fees, funding, unrealized P&L, collateral, leverage,
liquidation, cash transfers or FX. Every returned report explicitly
sets `risk_ready=false`, `equity_reconciled=false`,
`source_authenticated=false` and `order_state_complete=false`.

## Durability

`PaperAccountSnapshotJournal(path, capture, account_id=...)` stores
every complete source-supplied snapshot in an append-only JSONL chain,
with event sequence, predecessor hash, frozen account/session header
and separate fsynced checkpoint. Source prefix hashes are checked on
every replay. Missing checkpoint, broken chain, stale/duplicate IDs,
reversed capture time, changed account, contradictory cash and
nonmatching signed positions **fail closed**. Nothing repairs history
or guesses an omitted event. A crash after fsync of the record but
before checkpoint publication makes the archive unavailable until
audited external recovery.

Reconciliation is intentionally O(history size) and should run in a
separate supervisory process, not in a high-frequency strategy
callback or Rust hot path. SHA-256 provides corruption detection only:
an attacker able to rewrite the archive, receipt source and checkpoints
can forge a consistent history.

## Required follow-up

1. Implement an independently captured NautilusTrader sandbox
   account-state producer with stable account, order and position IDs,
   full open orders and remaining quantities, fill/fee/source chain
   and explicit session reset/flat-start evidence.
2. Add real capture timestamps and snapshot/receipt synchronization;
   test lost events, partial fills, position marks, corrections,
   disconnect, restart with open inventory, concurrent readers and
   kill-switch behavior.
3. Bind raw market quotes, funding and cash settlements to economic
   events, with per-venue margin/tier/liquidation and risk-budget
   checks before any P&L journal promotion.
4. Only after qualification, use a reconciled risk-capacity value for
   order sizing; snapshot validation alone cannot authorize orders.

To run the portable tests in the existing Debian/Termux environment:

```bash
PYTHONPATH=src:. python -m pytest tests/test_profit_allocation.py tests/test_profit_journal.py tests/test_position_reconcile.py tests/test_paper_evidence.py tests/test_sandbox_capture.py tests/test_paper_account_snapshot.py -q
```

This is paper/research only. No profitable trading strategy has been
qualified or real exchange order permissions enabled.
