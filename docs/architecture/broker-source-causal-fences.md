# ADR extension: broker-source causal cursor fences (offline)

**Status:** engine-independent, read-only *reported-evidence* integrity checker.
**Not** a custody attestation, broker connection, broker authentication, live
market-data validator, standalone exchange engine or safe inventory release.

## New code and test

`src/lab/kernel/source_fence.py` adds an immutable three-stream source
watermark contract. All source snapshots must explicitly name the
`account_scope`, source ID, venue, environment, instrument ID,
corporate-action version, stream epoch and source snapshot identity.

The event streams are **orders, executions and positions**. Each has a
caller-supplied prior `CursorAnchor`, contiguous integer source-frame
sequences and a resulting final anchor. Hashes commit each source frame to
its predecessor and the complete account/source/environment/epoch/instrument
scope, not just to an unqualified opaque event ID.

`assess_fenced_report` checks *reported* consistency, including:

- All three initial and final anchor channels present in canonical order.
- Initial cursors exactly matching **caller-supplied** previous anchors.
- Each event sequence exactly its predecessor's +1; no gaps, reorder, repeated
  event IDs or broken predecessor digests.
- SHA-256 integrity for every **claimed** source-frame payload and digest.
- Monotonic observed/available timestamps within each channel, and no event
  exchange time or local availability after the snapshot's stated boundary.
- Full deterministic SHA-256 binding to the exact reported orders,
  executions, positions, coverage assertions, source identity, timestamps
  and instrument version, so a snapshot cannot be swapped under an existing
  fence.
- The existing independent OMS comparison of order IDs, cumulative and
  individual fills, pending cancels, state freeze, action-version mismatch
  and current committed SQLite journal head.

This is fully standard-library Python; no Nautilus, scientific libraries,
sockets, broker credentials or actual orders. It uses the original
`OfflineOrderSession` and `ReportedSnapshot` without modifying either.
No new database or cross-store atomicity hazard is introduced.

## Critical boundary: internally consistent is not authenticated

A hash chain does **not** prove that a broker published the data, that a
snapshot covered all outstanding orders, or that the supplier didn't omit
events before the first anchor. This module publishes `frame_sha256` as a
**public deterministic function**, not a cryptographic signature. A
malicious or mistaken reporter can construct internally consistent but false
data. Prior anchors are also caller-supplied and **not trusted source state**
unless separately authenticated and durably pinned by a future adapter.

A `source_claims_complete=True` assertion is not evidence of verified broker
coverage. Frames currently carry hashes of opaque claimed payloads, not
broker-specific decoded `execution_id` / `order_id` updates; source-frame
contents are **not** yet linked to the full broker's native order/fill ledger.
This requires a separately written and reviewed provider-specific source
decoder. No evidence of the true market, exchange or regulator is inferred.

For these reasons:
- The verifier explicitly flags `paper` and `live` source environments as
  `EXTERNAL_BROKER_ENVIRONMENT_UNSUPPORTED`.
- Even with zero findings, its only positive claim is
  `internally_consistent` **for the supplied offline fixture**.
- `source_authenticated`, `broker_authorization` and
  `release_reservations` are permanently read-only `False` properties.
- This module never changes the OMS session, the independent double-entry
  financial accounts, reservations or external broker orders.
- Old/source-incompatible event journals are not silently migrated.

## Required real-broker integration milestones

1. Implement one broker provider's read-only, account-scoped **paper**
   login and verify authenticated account/venue/environment identity.
2. Pin the provider's source-sequence epoch and cursor bounds in a durable,
   scoped journal with an independently verified adapter state.
3. Confirm order/execution/position history completeness: include
   cancellations, old/closed GTC orders, unknown submissions, late fills,
   splits, and the account's cash, fees, margin and borrow rules.
4. Bind each decoded source event and actual broker order/execution ID to
   immutable frame bytes. Use provider documentation/API rather than
   inventing unsupported broker cursor or sequence fields.
5. Chaos-test reconnect, rollback, cursor gap, duplicate, unknown ack,
   split/version changes, crash after append and late fills across paper
   sessions; reconcile independent financial ledger and actual broker cash.
6. Only then design a separately audited **release-reservation** event and
   supervised paper authority. No fast path from successful reported-data
   parity to capital-moving authorization.

## Portable validation

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/causal-source-fence-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

`tests/test_portable_source_fence.py` has 18 cases, including valid three
interleaved source streams, gaps, predecessor breaks, duplicate source IDs,
changed snapshot fingerprints, account/epoch scope, absent source coverage,
late timestamps, cursor resets and no-Nautilus imports. Added to the
operator-accepted 102-test reference suite, **120 tests expected**, subject
to actual CI and independent on-device execution.

**Release gates:** F1/F3/F4/F6/F8 still BLOCKED. No code here enables money
movement, order placement, native equity corporate-action settlement or
production authentication.
