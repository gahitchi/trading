# Trading Lab — complete foundation-first development roadmap

**Audience:** contributors, reviewers, operators and researchers. **Status:** planning and qualification contract, not trading authorization. **Architecture:** independent Trading Lab engine; NautilusTrader is a reference/comparator only.

## 1. What we are building

Trading Lab is a reproducible research-to-execution platform. A user should eventually be able to install it, connect approved data and a supported broker, select a clearly bounded research strategy and risk profile, and start a *supervised* paper session through a simple interface. Autonomous real-money trading is a separate future authorization decision, not an automatic consequence of software completeness.

The goal is **institutional-quality engineering**: traceable inputs, exact economic accounting, causal experiments, strict exposure limits, deterministic replay, independent reconciliation, recoverable operations and evidence-based release. It does **not** mean claiming institutional performance, guaranteed returns, or supporting every asset on day one.

### 1.1 Product boundaries

| First supported slice (MVP) | Later expansion, after its own acceptance gates |
|---|---|
| One explicitly specified USD cash account and one broker-independent simulator | Multi-currency margin, shorts, leverage, derivatives and financing |
| One qualified point-in-time market-data source | Multiple data vendors, equities/FX/crypto venue adapters |
| One deterministic strategy with documented parameters | AI-generated hypotheses, portfolio optimizers and advanced strategies |
| Explicit conservative risk rules and kill switch | User-selectable higher-risk profiles, with additional constraints |
| Exact economic event journal, replay and read-only reconciliation | Automated cross-venue settlement and corporate-action workflows |
| Reproducible experiment reports and a simple operator interface | Distributed orchestration, complex dashboards and autonomous operations |
| External PAPER only after separate authenticated acceptance | Real-money trading only after independent, explicit authorization |

**Implementation strategy:** keep existing Python research software isolated as a comparison/migration source. Develop the independent Rust engine in its own branch and qualify behavior against engine-neutral contracts; do not make NautilusTrader's API the architecture. Reuse high-quality open-source methods, with license review and independent tests, not copied runtime assumptions.

## 2. How to read and execute this roadmap

**Hierarchy:** Phase → workstream → deliverable → verification → exit gate. IDs such as **F1.3** identify a workstream; subtasks such as **F1.3.a** are ordered actions. A completed implementation is not the same as a qualified milestone.

Each workstream must provide: (1) an owned implementation, (2) adversarial automated tests, (3) a reproducible evidence artifact, (4) documented failure/recovery behavior, and (5) review of assumptions and dependencies. CI success is necessary but not sufficient for a gate to be marked qualified.

**Dependency chain:**

`F0 Foundation → F1 Economics → F2 Data → F3 Decisions/Risk → F4 Orders → F5 Science → F6 External PAPER → F7 Deployment → F8 Release`

Implementation can proceed in parallel when interfaces are stable. **Qualification cannot skip upstream gates.** Every stage is currently **BLOCKED** until independently evidenced and signed off. Runtime policy remains **PAPER_ONLY**; no code merge silently authorizes broker activity.

## 3. Phase F0 — Reproducible foundation

**Objective:** anyone reviewing the same candidate can identify and reproduce precisely what was built and tested.

### F0.1 Repository and collaboration governance
- **F0.1.a** Define branch ownership: `main` under Claude integration; `chatgpt/*` under ChatGPT; preserve other agents' work.
- **F0.1.b** Require commit provenance, reviewable diffs, validated collaboration messages and clean merge history.
- **F0.1.c** Maintain a single versioned contract map and acceptance evidence index.
- **Verify:** branch protection, collaboration validator, clean-tree check, review trail.

### F0.2 Dependency and build provenance
- **F0.2.a** Pin Python, Rust, OS, container and native dependencies; record lockfile digests.
- **F0.2.b** Generate source-to-artifact receipts, SBOM, vulnerability scan and reproducible build instructions.
- **F0.2.c** Compare independently built artifacts and explain any nondeterminism.
- **Verify:** two clean builds, artifact hash comparison, strict supply-chain scan.

### F0.3 Deterministic test environment
- **F0.3.a** Fix clocks, seeds, locale, time zone and numeric precision; prohibit hidden network dependencies in unit tests.
- **F0.3.b** Establish offline golden fixtures and failure-injection harnesses.
- **F0.3.c** Require identical semantic outputs across supported runtimes and machines.
- **Verify:** repeated CI, Arch acceptance, deterministic replay digests.

### F0.4–F0.6 Deployment and governance baseline
- Validate native Arch and eventual Windows install, safe local state directories, permissions, crash logs, startup/shutdown and read-only health checks.
- Preserve strict CVE gates and no-secrets-in-logs rules; no exception solely because the product is research software.
- **F0 exit:** independently attested exact candidate; all governance, reproducibility and security checks green. **No trading authorization.**

## 4. Phase F1 — Exact economic conservation

**Objective:** every economic change has an identified cause and an independently checkable accounting effect. This is the current primary development focus.

### F1.1 Owned economic ledger
- **F1.1.a** Define immutable account, instrument, currency, position, lot, balance and posting types.
- **F1.1.b** Use exact bounded decimal arithmetic; double-entry conservation and deterministic FIFO realized P&L.
- **F1.1.c** Explicitly account for commissions, realized/unrealized P&L, cash and position changes.
- **Verify:** balanced postings, precision traps, long/short and round-trip examples, adversarial rounding.

### F1.2 Canonical event facts and provenance
- **F1.2.a** Own typed fills, cash movements, splits and broker-receipt facts independent of Nautilus.
- **F1.2.b** Require unique source and event identities, timestamps, account/instrument scope and bounded original bytes.
- **F1.2.c** Reject duplicates, contradictory facts, stale observations, unsupported schema and fabricated event identity.
- **Verify:** duplicate/reorder/replay and malformed-source tests.

### F1.3 Complete economic semantics
- **F1.3.a** Model dividends, withholding, fees, borrow interest, financing and interest with explicit event kinds.
- **F1.3.b** Model splits/reverse splits, fractional shares and cash-in-lieu without synthetic trades or invented liquidity.
- **F1.3.c** Define supported account/currency/instrument matrix; fail closed for unsupported settlement, FX, margin or corporate-action cases.
- **Verify:** independent hand-calculated examples, conservation and corporate-action edge cases.

### F1.4 Durable journal and restart
- **F1.4.a** Persist original receipts and canonical events atomically with a tamper-evident chain.
- **F1.4.b** Prove exactly-once effects across retries, crash-before/after-commit and repeated restarts.
- **F1.4.c** Rebuild account and positions from journal without consulting mutable strategy state.
- **Verify:** kill/restart/fault-injection tests and byte-for-byte replay evidence.

### F1.5 Independent account and order reconciliation
- **F1.5.a** Ingest independently captured broker account cash, positions and execution receipts.
- **F1.5.b** Cross-check complete order status (accepted, partial, filled, canceled, rejected), requested/executed/open quantities and each broker fill ID.
- **F1.5.c** Refuse incomplete scopes, unknown orders, overfills, stale snapshots, unmatched fees, missing events and ambiguous corporate actions.
- **F1.5.d** Report exact deltas without automatically modifying the ledger to match the broker.
- **Verify:** external-source custody review, adversarial fixtures and separate arithmetic oracle; no self-generated snapshot counted as independent evidence.

### F1.6 Evidence and exit
- Publish a reproducible F1 acceptance report: exact commit, hashes, supported scope, test matrix, residual risks and independent reviewer.
- **F1 exit:** all F1.1–F1.5 verified against independently sourced evidence and reviewed on the exact candidate. Passing isolated tests or the read-only CLI **does not qualify F1**.

## 5. Phase F2 — Causal, point-in-time market data

### F2.1 Source contracts
- Vendor identity, license, schema, instrument mapping, exchange/calendar, adjustments, currency and provenance.
- Raw immutable capture and checksum; explicit invalid/late/missing/duplicate data handling.

### F2.2 Time and universe causality
- Separate event, publication, ingestion and strategy-availability timestamps.
- Enforce no look-ahead across bars, splits, dividends, listings/delistings and membership changes.
- Keep delisted instruments and historical symbol mappings in the research universe.

### F2.3 Validation and exit
- Golden data snapshots, timestamp-boundary attacks, survivorship-bias tests, cross-source comparison.
- **F2 exit:** independent audit demonstrates decisions cannot see information before availability.

## 6. Phase F3 — Typed decisions, portfolio and risk

### F3.1 Decision pipeline
- Typed strategy signal → proposed intent → policy evaluation → approved/rejected intent.
- Explicit strategy/version/data/clock identifiers and deterministic decision trace.

### F3.2 Portfolio constraints
- Cash, buying power, fees, concentration, gross/net exposure, pending orders, currency and account limits.
- Reserve exposure **before** submitting an order; account for partial fills, cancel uncertainty and stale balances.
- Deny unsupported leverage, shorting, financing or cross-asset netting.

### F3.3 Operator safety
- Kill switch, reduce-only mode, stale-data halt, loss/drawdown limits, explicit operator confirmation.
- Never convert uncertain order state into permission for additional risk.

### F3.4 Exit
- Property tests, model-based state transitions, limit-boundary and race-condition adversarial tests.
- **F3 exit:** risk invariants hold under every supported decision and uncertainty transition.

## 7. Phase F4 — Durable order lifecycle and execution fidelity

### F4.1 Owned order state machine
- Intent → approved → submitted → acknowledged → partial → terminal (filled/canceled/rejected/expired).
- Persist client/broker IDs, idempotency keys, fill identities, venue/account scope and monotonic transitions.
- Reconnect/restart must not duplicate orders or infer cancellations from missing acknowledgments.

### F4.2 Broker-neutral adapters
- A typed adapter boundary for data, account observations, order submissions, cancel/replace and receipts.
- Simulator and real broker share semantic contracts but not invented broker guarantees.
- Unsupported broker/account behavior is explicitly blocked, never approximated silently.

### F4.3 Realistic simulation
- Bid/ask, spreads, commissions, slippage, latency, partial fills, volume constraints, market hours and funding.
- Never synthesize a trade to model a split, delisting or non-trade settlement.

### F4.4 Exit
- Restart/reconnect/replay chaos tests, unknown-outcome order tests and independent order reconciliation.
- **F4 exit:** no duplicate execution and no exposure increase under ambiguous order outcomes.

## 8. Phase F5 — Scientific research validation

### F5.1 Reproducible experiment specification
- Freeze data snapshot, strategy code, parameters, fees, clock, random seed and environment.
- Store immutable runs, complete trade/economic journals and comparable reports.

### F5.2 Statistical validity
- Train/validation/test and walk-forward separation; leakage and overfitting detection.
- Selection-aware multiple-testing controls, regime sensitivity and transaction-cost stress.
- Report drawdown, tail risk, turnover, capacity, uncertainty and negative results.

### F5.3 Exit
- Independent rerun of exact results and an adversarial challenge to the research claims.
- **F5 exit:** conclusions are reproducible and limitations visible, not merely profitable in backtests.

## 9. Phase F6 — Authenticated external PAPER acceptance

### F6.1 Safe onboarding
- Explicit operator credentials and broker account binding; secure secret storage and read-only discovery.
- Confirm paper environment, permissions, instrument/account scope, risk limits and kill switch.

### F6.2 Broker acceptance scenarios B01–B10
- **B01** identity and permissions; **B02** source timestamp/account scope; **B03** order acceptance and ID mapping.
- **B04** partial fills; **B05** cancel/replace race; **B06** rejection/expiry; **B07** disconnect/reconnect.
- **B08** crash/restart and duplicate suppression; **B09** account/cash/position/fee reconciliation; **B10** kill switch, stale-state halt and supervised recovery.
- Each case needs captured external receipts, expected transitions, exact ledger deltas and explicit failure criteria.

### F6.3 Exit
- Controlled supervised PAPER soak, independent broker receipt review and no unresolved account/order drift.
- **F6 exit:** all B01–B10 pass on the same candidate. No real-money permission.

## 10. Phase F7 — Operator-grade deployment

### F7.1 Plug-and-play installation
- Native Arch first, Windows next; reproducible signed packages, minimal guided setup and dependency diagnostics.
- Separate profiles for research, simulator and PAPER; no implicit live mode.

### F7.2 Operational reliability
- Structured logs, metrics, safe state directories, backups, migration/rollback, secrets protection.
- Explicit service status, safe shutdown, restart reconciliation, disaster-recovery drill and halt visibility.

### F7.3 Exit
- Clean-machine install, upgrade, rollback, restart and operator usability acceptance; monitored soak.
- **F7 exit:** independent operator can install, diagnose and recover without undocumented steps.

## 11. Phase F8 — Independent release

### F8.1 Seven release gates
1. Source and supply-chain provenance independently accepted.
2. Economic conservation and broker/account reconciliation independently accepted.
3. Point-in-time data and experiment causality accepted.
4. Risk invariants and kill/reduce-only behavior accepted.
5. Durable order lifecycle and restart behavior accepted.
6. External PAPER B01–B10 and operational soak accepted.
7. Security, deployment, documentation and operator review accepted.

### F8.2 Release procedure
- Freeze exact commit, lockfiles, artifact digests and approved supported feature matrix.
- Collect signed reviewer decisions, reproducible evidence links and known limitations.
- Publish only the authorized mode; changes to broker, instrument, leverage or live permissions require a **new** acceptance decision.
- **F8 exit:** all seven gates explicitly approved; no self-certification by CI or an AI agent.

## 12. Cross-cutting quality standards

**Architecture:** small, typed, auditable interfaces; single source of economic truth; independent comparison oracles; Rust core without Nautilus dependency; Python research interoperability only through versioned contracts.

**Security:** least privilege, encrypted secrets, no silent network calls, safe file permissions, input size/complexity limits, dependency CVE hard stops and explicit separation of paper/live credentials.

**Testing:** unit + property + differential + fuzz + integration + restart/chaos + Arch acceptance; deterministic fixtures and coverage of rejected/ambiguous states, not only happy paths.

**Numerics:** exact Decimal or checked fixed-point for accounting, defined rounding rules, no float-to-money conversion, independent conservation checks and precision traps.

**Operations:** actionable errors, operator-visible halt reasons, evidence preservation and no auto-reconciliation of financial discrepancies.

**Governance:** preserve `COLLABORATION.md`, own branches and review responsibilities. A merge is not a qualification; a green test is not release authority.

## 13. Current evidence and next actions

The machine-readable [F0–F8 contract map](f0-contract-map.json) is the source for **formal gate statuses**, not this prose document. Existing Python F1 research branches contain a pure ledger, typed events, durable SQLite journal, cash-in-lieu handling, independent account projection, original broker-fill receipt parsing and read-only account/order comparison. These are **candidate components**, not evidence of external broker custody, formal F1 completion or Rust-engine integration.

**Immediate sequence:** finish F0 signoff blockers → validate F1 event and settlement semantics → prove durable restart and independent broker receipt/order custody → perform external arithmetic and source audit → formally review F1 → proceed to F2 qualification. The new Rust implementation should implement the same owned contracts and pass its own equivalence tests; Python CI cannot qualify Rust by implication.

### Further reading

- [Machine-readable stage contract](f0-contract-map.json)
- [Independent-engine architecture decision](adr-independent-engine-2026-10-27.md)
- [First institutional-quality MVP cutline](mvp-f1-acceptance-cutline-2026-10-27.md)
