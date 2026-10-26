# Offline payload-bound causal source evidence

Status: **offline reference kernel only**. This is a prerequisite for, **not**
an implementation of, authenticated IBKR paper-broker reconciliation.

## What was added

The prior source-fence verifier checks account/source identity, explicit
venue/reconnect epoch, three cursor chains, frame-hash continuity and reported
snapshot parity. Its hashes previously referred only to opaque values.
The new lab.kernel.source_payloads module adds a narrow full-replay check:

1. The caller supplies immutable CapturedPayload objects in canonical,
   bounded ASCII JSON form. A payload contains exactly one
   typed position, order state or execution; quantities are exact Decimals.
2. Each (channel, sequence, event_id) must correspond to exactly one
   announced source frame, and the bytes must match the claimed SHA-256.
3. Decoded last order states, execution IDs/amounts and last position must
   match the full reported snapshot. An execution's exact exchange and
   availability timestamps must also match the source-frame metadata.
4. Replays not starting from three zero-sequence, zero-digest genesis cursors
   fail closed. A prior nonzero cursor is not a trusted predecessor snapshot.
5. All existing source-fence/parity findings are preserved; results have
   source_authenticated, broker_authorization and release_reservations
   hard-coded to **False**. No account mutation or transport exists.

The implementation intentionally does **not** claim the raw event schema is
a real brokerage wire format. It is a portable decoded *reference schema* for
future source-specific adapters. A self-generated, rehashed forged history can
pass this check and **still proves nothing about broker origin or completeness**.

## Remaining external integration gate

A future broker-specific collector must independently prove it is attached to
the configured **paper** account and correct execution environment, handle
synchronization of asynchronous positions/orders/executions with an explicit
consistency barrier, and journal the exact bytes it actually receives before
processing. Reconnect resets, duplicate callback IDs, late fills, partial fills,
pending cancellations, disconnected sessions, incomplete historical execution
availability and cross-account activity must all quarantine the account until
an independently reviewed state recovery procedure succeeds.

In particular, the IBKR API does not promise that independently received
positions, completed orders and executions share one atomic point-in-time
snapshot or a gapless, provider-signed global sequence. A local synthetic
counter or operator assertion cannot substitute for those guarantees.
Source-side authenticated transport also does not authenticate a previously
exported file. Neither these functions nor the upstream source fence should
be wired to release reservations, enable strategies or route orders.

## Acceptance

Run on the exact branch SHA, at minimum:

- PYTHONPATH=src python -m pytest -q tests/test_portable_source_payloads.py
- PYTHONPATH=src python -m lab.mobile check
- ruff check src tests tools
- ruff format --check src tests tools
- python tools/collab.py check

CI, independent adversarial review and native Arch supervised read-only
broker evidence are separately required; do not infer one from another.
