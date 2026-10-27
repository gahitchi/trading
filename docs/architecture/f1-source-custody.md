# F1 independent-engine evidence custody and settled-cash intake

**2026-10-27. Candidate branch:** `chatgpt/f1-evidence-custody-20261027`.

**Qualification:** BLOCKED. Read-only/offline research implementation. Nothing
here grants release authority, claims authenticated external data or changes
the currently Nautilus-driven legacy execution path.

## What this branch implements

1. The canonical `EngineEconomicBook` remains **Trading Lab owned** and
   depends on standard-library Decimal/accounting code, **not NautilusTrader**.
2. Every appended economic fact contributes to a SHA-256 evidence chain
   combining the preceding head, own-account ID, event ID, independent source
   reference, nanosecond event timestamp, typed event kind and exact values.
   This covers fill economics, dividend/carry/capital events and split ratios.
3. The snapshot contract includes an explicitly supplied
   `applied_evidence_head`. A matching list of trade IDs alone no longer
   proves that a snapshot has observed the same provenance/amount/time input.
   `audit()` independently replays all events and compares both the
   economic postings and the complete source-evidence head.
4. A `CashFact` requires a **declared SETTLED** status, a supported source
   category matching the economic kind, a stable statement-record ID and a
   lowercase SHA-256 digest. `EXPECTED`, `ESTIMATED`, `PENDING`,
   foreign-currency amounts, invalid source types and unexplained cash deltas
   cannot silently appear as settled ledger entries.
5. `settlement_input.py` accepts one bounded UTF-8 JSON activity record,
   rejects duplicate fields, unknown schema keys, floats/nonfinite amounts,
   unsupported currencies and account mismatch, hashes the **original
   exact bytes**, and converts it to the canonical cash fact. It does not
   invent market transactions or inject fake executable liquidity.

These are technical **integrity and classification** checks. An operator,
source adapter or attacker can still supply a fake `SETTLED` statement
and a matching SHA-256 of those fake bytes. SHA-256 does not authenticate
a broker or guarantee a transaction actually settled.

## Remaining acceptance work

- F1.2: real independent engine account state and externally captured
  order/fill streams (rather than snapshots constructed from journal fields);
  checks for complete instrument scope and independent event ordering.
- F1.3: authenticated raw statement origin and reconciliation to cash
  entitlements, accrual vs settlement, withholding, distributions on short
  positions, financing rebates, sub-cent rounding/custody and fractional
  corporate actions. The current cash parser supports **only five** kinds
  with strictly positive absolute amounts and predeclared directions.
- F1.4: atomic append-only durable custody across original source bytes,
  economic postings and native engine state; durable idempotency,
  correction/supersession, snapshot checkpoints, journal signatures or
  independently anchored digests. The current unkeyed chain can be fully
  rewritten by a party with unrestricted write access.
- F1.5: independent arithmetic scenarios, financial spot checks,
  adversity/failure cases, restart parity and reviewer signoff.
- F0 signoff and separate F6/F8 broker/release gates remain mandatory.

## Developer validation

```sh
uv sync --frozen --python 3.13 --extra dev
uv run --no-sync pytest -q tests/test_economic_ledger.py tests/test_engine_events.py tests/test_settlement_input.py
uvx --from ruff==0.17.3 ruff check src/lab/economic_ledger.py src/lab/engine_events.py src/lab/settlement_input.py tests/test_economic_ledger.py tests/test_engine_events.py tests/test_settlement_input.py
uvx --from ruff==0.17.3 ruff format --check src/lab/economic_ledger.py src/lab/engine_events.py src/lab/settlement_input.py tests/test_economic_ledger.py tests/test_engine_events.py tests/test_settlement_input.py
```

This branch explicitly does not modify `src/lab/agents/execution.py`,
`src/lab/paper.py`, the broker order/risk path, the hard-stop rules or
any existing Docker vulnerability gate.
