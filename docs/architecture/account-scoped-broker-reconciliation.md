# Offline broker-reconciliation account scopes — v2 contract

**Status:** engine-independent reference and negative tests only. No account
authentication, network transport, trading permission, source signatures,
broker settlement or automatic reservation release.

## Source and recovery contract

The read-only broker reconciliation component previously compared position,
fill and order history to a matching instrument/version but could compare a
report from a **different brokerage account** because the offline OMS had no
account scope. This is an unsafe identity ambiguity even in a simulator.

The revised `OfflineOrderSession` constructor **requires** a stable explicit
`account_scope`, stored in the immutable journal session header together
with the instrument definition version, starting position and lot size.
The starting position remains an **operator assertion**: an account-scope
string is not authenticated custody. All `BrokerEvidence` records must now
have that same account scope; journal replay validates it. The reported
`ReportedSnapshot.account_scope` must agree with the session before the
comparison can be considered internally consistent.

There is **no implicit default account**. The order journal uses
`session:independent-order:v2` and `schema=2` and hashes the scope into
the independent state projection. Existing `v1` order journal files are
explicitly rejected, not re-labeled or automatically upgraded. Any future
migration requires authenticated account binding and a separately audited
full replay into a new evidence-backed journal.

The comparison also detects
`BROKER_ORDER_ID_UNACKNOWLEDGED_LOCALLY` when a prepared local order has
no journaled acknowledgement yet a broker snapshot reports a broker order
ID. This is a **danger/unknown** rather than a reason to adopt the ID and
continue automatically. The existing SQLite journal-head transaction guard
continues to detect stale competing sessions before even returning success
on an idempotent event.

## What it does not prove

Neither an explicitly provided `account_scope`, a SHA-256 of an export, nor
`positions_complete=True` establishes identity/authority. Those are
untrusted claims. The reconciliation result's three authorization properties
are always read-only `False`:

- `source_authenticated`
- `broker_authorization`
- `release_reservations`

To move this into **supervised external paper trading**, implement a
broker-specific authenticated account namespace and a **broker-sourced**
complete, causal, cursor/sequence-fenced set of order, execution and position
snapshots. Include all GTC orders, pending cancellations, unknown placements,
late fills, per-account execution identity, margin/borrow, and settlement cash.
No new orders or reservation release before that qualification. Failure of a
broker re-authentication, account approval check or cursor gap freezes the
system. The independent financial ledger remains the economic projection and
NautilusTrader a differential oracle, not a replacement for broker evidence.

## Portable test entry

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/account-scoped-reconciliation-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

The additional tests reject cross-account events and copies of otherwise
matching reported snapshots, missing broker-order acknowledgements,
replay against a different account, and attempting to upgrade an unscoped v1
order stream. This remains **offline-only**; F3/F4/F6 and F8 release gates
remain blocked. No real broker keys, paper account numbers or live orders
are required.
