# First viable product: a smaller scope, unchanged qualification standards

**Decision:** 2026-10-27. **Status:** implementation plan, not release approval.

The first viable product (MVP) is an offline, reproducible, deterministic and
**paper-only** trading research tool built on **Trading Lab's own** economic
and execution models. High institutional quality means strict arithmetic,
causal ordering, durable evidence, independent acceptance and fail-closed
operator controls. It does *not* mean the MVP must implement every planned
research technique or autonomous AI before it can be used.

## MVP scope, in roadmap order

- **F0:** exact-source builds, deterministic test environment, operator
  reproducibility, dependency/security provenance. Release sign-off separate.
- **F1:** a single USD cash-account economic book initially, with exact
  Decimal FIFO long/short accounting; deterministic typed events and
  commission/settlement classifications; durable atomic replay and restart;
  independent rational arithmetic checks; read-only external cash/positions
  comparisons. No source-less cash adjustment. Broker-origin authentication
  remains an explicit open gate rather than a fabricated guarantee.
- **F2:** one explicit, immutable bar-data import route with strict timestamps,
  instrument identity, missing-data refusal, source provenance and no
  look-ahead. Start with one liquid stock and add universes later.
- **F3:** one deterministic, configurable rule-based strategy and conservative
  position/portfolio risk limits, reviewed reproducibility and no LLM.
- **F4:** own simulated order lifecycle: accepted/rejected/canceled/partial/
  fully-filled, next-bar executable pricing, bounded costs, durable state,
  restart and disagreement hard-stop. Paper brokers are adapters, never the
  economic core.
- **F5:** results record the strategy, complete inputs, time boundaries,
  fills, cash/positions, costs, independent control, and reproducible
  audit artifacts. A research result is never claimed predictive solely
  because a backtest is profitable.
- **F6:** authenticated *external* paper trading behind independently
  approved connection, account-scope, order and recovery tests.
- **F7/F8:** reliable installer/operator journey and full fixed-candidate
  release verification. Docker CVE findings are not waived.

## Deferred until *after* the first qualifying viable product

Autonomous AI analysis, multi-agent swarms, broad multi-venue support,
complex portfolio optimizers, sophisticated strategy search, user-facing
prediction claims and unnecessary dashboard embellishments. None are F1
accounting prerequisites and none can substitute for a working F2–F4 engine.

## Current operator-visible F1 diagnostic

```sh
uv run --no-sync python -m lab.f1_verify \
  --journal ./economic.sqlite3 --account SIM-001 \
  --statement ./external-account.json --source broker-export
```

The journal file must **already exist**; the command cannot create it or
send orders. A complete USD report is required. Output is machine-readable
JSON with exact cash/quantity differences and evidence digests; exit codes:
0 numeric match, 2 discrepancy, 3 invalid or unavailable evidence. Even
a match explicitly sets `broker_source_authenticated=false`,
`execution_authorized=false` and `release_authorized=false`.

### Current development blockers, not waivers

- The current MVP diagnostic does not generate an account statement itself.
  A genuine independent adapter and statement-source authentication remain.
- F1 typed fill inputs are not yet independently broker-attested or
  atomically bound to the complete native order lifecycle.
- The journal's replay-first append is not efficient at large history length;
  do not deploy an unqualified high-throughput runtime on it.
- Broader corporate actions, actual broker rounding/withholding, external
  corrections, complete-account/margin scope and crash/disconnect races
  remain F1 sign-off requirements for affected supported capabilities.
- Signing an independently anchored audit root, privacy handling, Docker
  CVE remediation and reviewer approval are not achieved by a local hash.

This is a strict **MVP cutline**, not permission to downgrade safeguards.
Unsupported market/accounts/events must reject instead of attempting to
silently simulate richer semantics. NautilusTrader is not imported by the
independent F1 economic modules; the older application still contains it.
