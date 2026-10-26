# Execution fill anomalies and terminal intent fencing

**Scope:** broker-neutral execution accounting hardening in the existing
paper-only Nautilus execution agent. No broker account is accessed, and no
permission to send an external order is introduced.

## Found and fixed

1. An individual fill could cause an active journaled order's accumulated
   `filled` quantity to exceed its submitted `qty` without a risk event.
   The execution ledger now books **the entire verified-scope reported fill**
   into cash, inventory and immutable trade-ID history (no clipping or
   invented reversal), records `overfilled: true`, then hard-halts with the
   order ID and quantities. Quantities are compared with a numerical
   tolerance for floating conversion, not an execution tolerance or order
   size relaxation.
2. A rejected mismatched/invalid report could be followed by an
   order-terminal event that removed the unresolved write-ahead intent.
   Now an invalid report latches `invalid_fill: true` in the journal and
   persists the flag before halting. Terminal callbacks and recovery cannot
   archive that intent; they remain blocked for operator investigation.
3. A replayed execution identifier naming a *different* client order could
   be mistaken for an ordinary duplicate. The engine now explicitly
   hard-halts without booking a second cash/inventory change. Recovery
   cannot use that conflicting ID to discharge an order's pending intent.
   Existing honest identical replays remain idempotent.

Both anomaly fields are strictly boolean in
`src/lab/execution_state.py`, regardless of pending/terminal/disposed
bucket, to reject malformed state during startup.

## Economic/accounting policy

- A valid, broker-scoped overfill **is not ignored**: the entire economic
  event must appear in the ledger. It creates a hard-stop requiring
  reconciliation rather than authorizing a compensating trade.
- A mismatched, invalid, foreign-account or duplicate-with-conflicting-order
  report **cannot** authorize a monetary entry or order closure.
- Any uncertain intent survives a cancelled/rejected/expired status and
  persists in the journal. A broker-status message alone does not
  prove the associated execution economics.
- The existing paper guard, kill switch, order-book authority, reserve
  policy and retry restrictions remain unchanged.

## Remaining limits (not release-qualified)

- Current applied execution IDs encode receipt time and client order ID,
  but **not economic terms** (price, commission, quantity, side, venue
  epoch) or an authenticated provider receipt. Two conflicting reports
  for the same trade ID and client order may still look like duplicates.
  Separate evidence-bound fill records are needed before supervised paper
  acceptance can be qualified.
- The ledger remains a client-side book rather than broker-confirmed
  account balances. Corporate actions, financing, settlement, margin
  currency and source coverage still require independent reconciliation.

## Test strategy

`tests/test_fill_integrity_halt.py` uses offline broker-event fakes
and a real local SQLite store to cover normal split fills, overfills,
near-equality, duplicate ID collision, malformed quantities/prices/sides,
cached terminal events and durability of the unresolved-intent latch.

New code is proposed on
`chatgpt/fill-integrity-halt-20261026`, branched from
`chatgpt/execution-recovery-fence-20261026` (G037). The G036 and
G037 parents must be reviewed independently before integration.
