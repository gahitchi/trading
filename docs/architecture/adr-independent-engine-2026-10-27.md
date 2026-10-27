# ADR — Trading Lab owns its engine; Nautilus is not its operating contract

**Decision:** 2026-10-27. **Status:** accepted owner direction; implementation and
independent qualification remain incomplete. **Applies to:** F1 onward.

## Superseded assumption

The inherited platform executes agents through NautilusTrader and earlier F1
gap reports ask for exact parity with `NautilusTrader.Portfolio`/`Cache`.
This must not be interpreted as a permanent dependency or as the canonical
authority over cash, positions, orders, financing or corporate actions.

The owner had already chosen a **broker-independent and engine-independent**
Trading Lab, informed by proven high-quality open-source implementations.
Building proprietary core behavior by adapting every Nautilus native type
contradicts this decision.

## Architectural contract

1. **Trading Lab defines and owns** canonical events, instrument identities,
   financial ledgers, portfolio/collateral state, typed decisions, risk
   reservations, order state transitions, durable evidence and recovery.
2. The economic core consumes **our typed facts**, with stable event/source
   identity, account identity, timestamps and exact Decimal arithmetic.
   `src/lab/economic_ledger.py` and `src/lab/engine_events.py` are the
   isolated F1.1/F1.2 starting point.
3. Market-data providers, venue and broker drivers, UI and optional execution
   runtimes become adapters with no write permission to canonical financial
   records except through validated typed event/settlement APIs.
4. **NautilusTrader is optional**: useful for scientific comparison,
   reference backtests and migration checks. No canonical F1 business object
   may require a NautilusTrader model, event, account or Python import.
   Compare behavior independently, not by accepting Nautilus accounting as
   the only possible account truth.
5. Reuse high-quality public techniques with license compliance, attribution,
   independently reproduced behavior and tests. No wholesale copying of
   implementation merely because a library is familiar.
6. For security, safety and correctness: no unclassified external cash
   adjustment, no fake executable trade to book corporate actions, no
   automatic paper or live authority, no implicit rounding, no unsafe
   margin-cash inference. The existing hard-stop and paper-only rules remain.

## Reconciliation semantics

F1 is exited only after **our** independent account/position/order books
conserve across all typed events, agree with separately observed external
broker/market records where applicable, replay identically across crashes,
handle fees/financing/splits without fabricated trades, and survive an
independent adversarial review. Nautilus parity is an *optional comparator*,
not a prerequisite that forces our engine to adopt its account model.

A comparator and simulator test may expose design mistakes, but two matching
engines can share the same conceptual bug. Ground truth requires independently
observed events and stable custody, not mutually generated snapshots.

## Development sequence (unchanged gate ordering)

- F0: reproducible exact source/build/operator evidence; sign-off remains
  independent and is not granted by unit tests.
- F1.1: pure balanced FIFO ledger and replay — implemented prototype.
- F1.2: **own engine typed economic input and exact account reconciliation**;
  the new `engine_events` slice is offline and not production-wired.
- F1.3: independent classified settlement, carry, dividends, splits,
  withholding, cash-in-lieu, rounding and sub-cent policy.
- F1.4: durable atomic event-source/ledger/order custody and restart.
- F1.5: real broker-neutral end-to-end parity and adversarial failure matrix.
- F2–F8: previously defined point-in-time, decision/risk, order lifecycle,
  scientific research, authenticated paper acceptance, deployment and release.

## Superseded exploratory branches

`chatgpt/f1-native-observation-20261027` and
`chatgpt/f1-account-state-evidence-20261027` explored direct Nautilus
state adaptation before the correction. **Do not integrate them as our
canonical F1.2 engine**. Their fixtures may be useful in a strictly separate
comparison/test adapter later. This ADR does not rewrite those branches,
touch another agent's code or change GitHub's existing Docker security gate.

**The F1 qualification remains BLOCKED.** This ADR is a development choice,
not proof of finished economics, release readiness or external broker
authorization.
