# F1 independent engine — evidence, settlement and durable accounting

**2026-10-27; implementation candidate:**
`chatgpt/f1-durable-economic-20261027` (supersedes the exploratory
`chatgpt/f1-evidence-custody-20261027` branch). **Qualification: BLOCKED.**
The source of truth is Trading Lab's broker-neutral model, NOT NautilusTrader.
All implementation here is offline research-only, with no order authority.

## Implemented prototype capabilities

- `economic_ledger.py`: exact-Decimal signed FIFO inventory, long/short
  fills, commissions, immutable double-entry postings and business-event replay.
  Nontrade splits and fractional cash-in-lieu adjust share quantity/cost basis
  without manufacturing a price print, executed trade or market volume.
- `engine_events.py`: own typed source facts (fill, classified cash,
  split, cash-in-lieu), stable source identity and account identity, strict
  timestamp ordering, duplicate rejection and source-evidence SHA-256 chaining.
- `settlement_input.py`: strict bounded JSON input for declared settled
  USD activity and fractional cash-in-lieu. Rejects duplicate/extra fields,
  wrong account/currency, float or nonfinite amounts and unsupported statuses.
  Original exact bytes are hashed. No broker authenticity is inferred.
- `economic_store.py`: SQLite WAL and FULL synchronization; transactional
  BEGIN IMMEDIATE, explicit commit, rollback/close, idempotent exact-evidence
  retries, event/source uniqueness, optimistic head fencing, replay-verified
  postings/source hashes and original source-byte storage for classified
  cash and cash-in-lieu.
- `account_projection.py`: independently applies source economic facts
  with rational `Fraction` arithmetic and exact bounded base-10 conversion.
  This mathematically cross-checks cash and net quantities without using
  journal postings or FIFO lots; it does not independently authenticate inputs.
- Settle-only posting types: capital in/out, dividend credits, interest
  credits, borrow rebates, dividend withholding, short-manufactured
  distributions, financing debits and borrow fees. No unsourced balance delta
  is treated as income, funding or principal.

## Exactness and custody constraints

- Every trade source must have unique ID, positive exact quantity/price
  and explicitly specified nonnegative commission; unsupported currencies
  are not silently converted.
- Each cash adjustment must have a declared economic kind and `SETTLED`
  source event. Modeled carry and expected entitlements must remain outside
  the settled account ledger.
- Cash-in-lieu supports strictly fractional quantities and exact FIFO
  basis release, with no implicit rounding. Non-terminating fractions are
  refused pending an externally reviewed remainder/rounding policy.
- Persisted financial payloads require bounded scale and precision; even
  extreme exponent strings cannot expand into unbounded serialized digits.
- Local hash chaining detects inconsistent in-place edits, but an attacker
  controlling the entire database can replace the entire history and
  unkeyed hash root. An independently anchored digest/signature is required.

## Additional F1.2/F1.4 implementation on source-binding candidate

The `chatgpt/f1-source-binding-20261027` branch adds the following strictly
separated and explicitly unqualified capabilities:

- **Source-content binding:** cash and fractional cash-in-lieu statement bytes
  are parsed again on every durable replay. An input fact with a valid
  SHA-256 digest but falsified amount, event time or classification is refused.
  A digest proves only byte equality, not issuer authenticity.
- **Broker-claimed fill receipts:** `fill_input.py` parses complete exact-USD
  original execution records with account, fill ID, source namespace,
  order ID, last quantity, last price, commission and executed status. Each
  execution, including partial executions of the same broker order, is an
  independent `BrokerFillFact` with an original-byte SHA-256, distinct from
  simulator `FillFact`. Original receipt bytes and accounting state commit
  atomically to the SQLite journal, and both are replay-validated. This does
  *not* authenticate the broker or prove an order was authorized.
- **Independent account statements:** `account_observation.py` parses
  a separate claimed-complete USD holdings/cash record; rejects foreign,
  stale, partial, nonfinite, float and duplicate-position records. It compares
  exact reported cash and net instruments against both our double-entry
  ledger and rational account projector; disagreement is reported, not
  automatically credited or written off. An input file can still be fake.
- **Read-only diagnostics:** `EconomicEventStore(..., readonly=True)` refuses
  append and cannot create a missing database. The standalone
  `python -m lab.f1_verify` emits machine-readable exact differences with
  codes 0 (arithmetic match), 2 (mismatch), or 3 (invalid/missing evidence).
  Its output always states broker authentication and execution/release
  authorization are false.

The MVP retains these strict core contracts, but may start with one liquid
USD cash-account instrument, one rule-based strategy and one offline broker-
neutral matching/simulation route. AI research, multi-agent automation and
complex portfolio methods are explicitly **not** required for this first
usable product. This reduces scope, not acceptance standards.

**F1 sign-off is still blocked** by genuine broker-origin verification,
separately reconciled order-state custody, full account completeness and
finality, corporate-action corrections, durable checkpoint/performance
qualification, explicit rounding-policy evidence, external source controls
and independent review. The isolated F1 tests do not authorize paper orders.

## Mandatory work before F1 sign-off

1. **F1.2:** separately acquired engine/account and externally captured
   broker transaction observations. The rational projector is a different
   arithmetic path over the same events, **not** an external accounting oracle.
   Account completeness, timestamp finality and independent fill capture
   must be validated against the same transaction IDs.
2. **F1.3:** currency-specific USD minor-unit and sub-cent remainders,
   native broker commission/fee classification, ex-/pay-date entitlements,
   tax jurisdiction, short distribution entitlement, mergers, complex
   reversals and delisting settlement. Cash-in-lieu is presently strictly
   fractional and cannot substitute for qualified delisting rules.
3. **F1.4:** durable stored broker order + raw fill source bytes, atomic
   broker observation/ledger/own-engine state commits, corrections and
   supersession (do not rewrite immutable originals), migrations/backfills,
   externally anchored evidence and bounded streaming performance.
   Current replay-based append is not suitable for very long histories:
   it rebuilds the journal and has superlinear cost.
4. **F1.5:** externally independent arithmetic reference and broker-state
   parity; actual partial-fill/cancel races, account-type and margin
   semantics, crashes at transactional boundaries, adversarial corruption,
   reproducible Arch results, source-custody review and separate owner sign-off.

F0's user-operated Arch baseline checks have passed, but F0 approval and
the strict Docker vulnerability gate are separate outstanding obligations.
Neither F1 nor F6 nor F8 can be marked approved by the present isolated tests.

## Development checks

```sh
uv sync --frozen --python 3.13 --extra dev
uv run --no-sync pytest -q tests/test_economic_ledger.py tests/test_engine_events.py tests/test_settlement_input.py tests/test_economic_store.py tests/test_account_projection.py tests/test_settlement_semantics.py tests/test_cash_in_lieu.py
uvx --from ruff==0.17.3 ruff check src tests tools
uvx --from ruff==0.17.3 ruff format --check src tests tools
python3 tools/collab.py check
```

This branch does not modify the legacy Nautilus-based execution agent,
paper-only broker gate, kill switch, independent release gates or Docker
security policy. It is a replacement-engine **foundation**, not a completed
trading engine or production-ready financial ledger.


## F1.5 receipt-complete order observation (Python candidate, 2026-10-28)

The read-only order reporter now accepts two explicit schemas. Legacy v1
checks **aggregate order quantities only**, and remains parseable for research
comparison. New v2 requires a bounded, complete per-order
`execution_receipts` inventory, each identifying the original fill
`record_id`, exact `quantity` and SHA-256 of the original execution report.
Each original `BrokerFillFact` must match a unique enumerated receipt,
including its account-scoped source reference, broker order ID, quantity,
and original evidence digest. An extra, missing or substituted receipt is a
hard refusal even when executed quantity totals agree. The complete inventory
is still an **unverified external claim**, not independently authenticated.

The `lab.f1_verify` CLI now requires v2 identity-complete order evidence
when a durable journal contains broker fills and requires the account and
order observations to share the exact `as_of_ns` boundary. Legacy v1
arithmetic remains available from the library with
`receipt_scope_checked=False`, but cannot cause a broker-fill CLI success.
No trading authorization, broker connection or fabricated order is added.

Pending: independently verified account/order report extraction, immutable
external source and completeness attestation, broker correction events,
per-order finality/races, exact source custody and independent review. F1
qualification remains BLOCKED.


## F1.4 immutable local order-observation custody (Python candidate)

`EconomicEventStore.capture_order_observation(...)` now accepts a
**complete v2 claimed-external order report** with the caller's explicit
expected economic-head fence. The original bounded UTF-8 JSON bytes, SHA-256,
source record identity, timestamp, economic event count and evidence head
commit in **one SQLite `BEGIN IMMEDIATE` transaction** to an append-only
observation table, separate from all economic postings. Matching retries
are idempotent; conflicting reuse, mismatched source facts, stale journal
heads and over-capacity archives are refused. There is no order submission.

`inspect_order_observations()` is read-only and replay-verifies each archived
report against the exact economic event prefix it originally observed.
Appending future unrelated events does not rewrite, invalidate or silently
upgrade previously observed broker claims. Corrupt stored originals or head
bindings cause an explicit refusal. This is local byte custody **only**:
SQLite and unkeyed SHA-256 cannot prove broker origin, resist privileged
database replacement, or replace independently anchored evidence. A later
migration must address encryption, lifecycle reconciliation, capture finality,
schema/version compatibility and bounded replay performance. F1 remains
BLOCKED and no broker mode is enabled.

A same-identity retry additionally rebuilds the originally recorded economic
prefix and verifies its saved head and complete individual execution receipt
inventory. A privileged actor cannot turn a previously corrupt snapshot into
an accepted retry by supplying a substituted local metadata head.
