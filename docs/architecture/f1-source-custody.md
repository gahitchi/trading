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
