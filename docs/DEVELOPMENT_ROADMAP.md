# Trading Lab — institutional-grade development tree

> **Working roadmap, 27 October 2026.** This is a planning and acceptance map, **not** a declaration of release readiness. It must be reviewed with the same collaboration protocol as code. Owner determines scope and priorities; Claude integrates reviewed changes into `main`; ChatGPT develops/reviews on `chatgpt/*`. The status of each node must cite a branch/commit, tests and independent decision before it can be changed to **qualified**.

## Status semantics

| Marker | Meaning |
|---|---|
| [M] | Relevant implementation exists on `main`. **Not** proof of production/broker qualification. |
| [C] | Candidate implementation exists on a separate branch; **not integrated** or independently accepted. |
| [P] | Partially implemented; further engineering or quantified validation needed. |
| [T] | Planned work: design and tests to be implemented. |
| [E] | Blocked on external evidence (broker, independent reviewer, operator or operating sessions). |
| [Q] | Independent acceptance complete for a particular exact candidate SHA, only with recorded evidence. |

The release criteria are **separate** from implementation states. A green CI run certifies the tested revision, not institutional readiness or trading profitability. An existing architecture, class, menu or adapter is not a completed milestone until its defined evidence and failure tests pass. Never downgrade risk, statistical or integrity gates merely to close a task.

## Hierarchical work breakdown structure

```text
TRADING LAB — reproducible research + controlled multi-market execution
│
├── R0  Governance, versioning and architectural baseline [P]
│   ├── R0.1 [M] Modular repo, unified Nautilus event-driven foundation
│   ├── R0.2 [M] Independent reviews, protected branch ownership, immutable outbox
│   ├── R0.3 [M] Candidate-specific release checker and evidence integrity
│   ├── R0.4 [P] Consolidate validated feature branches into reviewed main
│   └── R0.5 [T] Architecture decisions and end-to-end traceability matrix
│
├── R1  Scientific research and statistical validity [P]
│   ├── R1.1 [M] Isolated signal research, frozen data and holdout exposure ledger
│   ├── R1.2 [P] Correct excess-return ratios and session accounting (F-R05)
│   ├── R1.3 [P] HAC PSR/DSR, correlated trials, robust scale (F-R01/02)
│   ├── R1.4 [P] Purged CV, embargo, stationary/bootstrap calibration (F-R03/04)
│   ├── R1.5 [P] Active-window walk-forward, factor tear-sheets (F-R14/15)
│   └── R1.6 [P] Reproducible strategy approvals and critical-code scope (F-R16)
│
├── R2  Market data, microstructure and instrument economics [P]
│   ├── R2.1 [P] Time-aware ingestion, raw-vs-normalized provenance and replay
│   ├── R2.2 [P] Intrabar fills, spread/impact/volume participation (F-R06/12)
│   ├── R2.3 [P] Splits, dividend/delisting, precision and fees (F-R07/09)
│   ├── R2.4 [P] Borrow/recall, collateral/cash sweep, FX funding (F-R08)
│   ├── R2.5 [P] Stocks + London Gold via implemented paper paths
│   ├── R2.6 [T] Qualified forex and crypto exchange/data adapters
│   └── R2.7 [T] Asset-specific time, precision, calendar and outage contracts
│
├── R3  Broker-neutral OMS, execution and accounting [P]
│   ├── R3.1 [M] Immutable intent/order/fill state and journal recovery primitives
│   ├── R3.2 [P] Exactly-once fills, pending-cancel/late-fill race coverage
│   ├── R3.3 [P] Independent multi-currency double-entry and cash/fee parity
│   ├── R3.4 [C] Read-only capture-to-OMS reported-state comparison
│   ├── R3.5 [T] Broker-history completeness, cursor and causality contracts
│   ├── R3.6 [P] Safe halt, reservation retention, disposal, restart reconciliation
│   └── R3.7 [T] Qualify router with deterministic fault-injection acceptance
│
├── R4  Portfolio construction and independent risk control [P]
│   ├── R4.1 [M] Named risk profiles and paper-only safety boundary
│   ├── R4.2 [P] Long/short/gross/net, margin and stressed pre-trade reservations
│   ├── R4.3 [P] Shrinkage, HRP, overlapping histories (F-R10/11)
│   ├── R4.4 [P] Reservation rebalancing and breach overrides (F-R13)
│   ├── R4.5 [P] Kill, flatten, manual approval and risk-budget enforcement
│   └── R4.6 [T] Broker-vs-model margin/borrow/cash verification per instrument
│
├── R5  Broker/exchange gateway qualification [E]
│   ├── R5.1 [C] B01 local read-only PAPER capture with explicit failure state
│   ├── R5.2 [C] Source-file coherence, exact balances, privacy and inspection
│   ├── R5.3 [E] Genuine authenticated broker identity and broker-data readiness
│   ├── R5.4 [E] Complete order/execution/position and causal history verification
│   ├── R5.5 [E] Broker cash/fee, financing and buying-power reconciliation
│   ├── R5.6 [T] Broker-neutral adapter conformance + disconnect/resume contract
│   └── R5.7 [T] Additional venue sandbox qualification before any new asset scope
│
├── R6  Adaptive strategy and operator authority [P]
│   ├── R6.1 [M] Approval artifacts, manual/supervised/autonomous mode primitives
│   ├── R6.2 [P] Eligibility, policy-wide testing, retirement, safe cash fallback
│   ├── R6.3 [P] Strategy-bank regime hysteresis and dwell (F-R17)
│   ├── R6.4 [T] Natural-language requests -> typed, reviewable trade settings
│   ├── R6.5 [T] Explicit human confirmation of requested risk/market changes
│   └── R6.6 [E] Demonstrate safe, authorized paper autonomy across disconnects
│
├── R7  Operator product, installer and experience [P]
│   ├── R7.1 [P] Arch/Windows installer, dependencies and upgrade/rollback path
│   ├── R7.2 [P] Unified setup: PAPER identity, broker mode, market, profile
│   ├── R7.3 [P] Dashboard: status, audit findings, risk, orders and cash
│   ├── R7.4 [T] One-click start only after explicit checks and approvals
│   ├── R7.5 [T] Structured trade-intent text box (R6.4) with dry run
│   └── R7.6 [T] Diagnostics usable on Termux without broker/native dependencies
│
├── R8  Production operations, testing and security [P]
│   ├── R8.1 [M] Pinned source, packaging, Docker, lint and dual-Python CI
│   ├── R8.2 [P] Fast/integration/real-clock tiers and coverage (F-R19)
│   ├── R8.3 [P] Crash/restart, torn writes, backups, migration and alerts
│   ├── R8.4 [T] Disk exhaustion, power-loss and prolonged growth/performance tests
│   ├── R8.5 [E] Five consecutive market sessions with reviewed incident evidence
│   └── R8.6 [E] Independent sandbox, container and secrets/security assessment
│
└── R9  Formal release qualification [E]
    ├── R9.1 [E] code-review
    ├── R9.2 [E] broker-lifecycle (B01–B09)
    ├── R9.3 [E] research-calibration
    ├── R9.4 [E] market-data-and-execution (B07–B10)
    ├── R9.5 [E] adaptive-product
    ├── R9.6 [E] operations-and-build
    └── R9.7 [E] independent-security
```

**Coverage convention:** A marker [M] says code is in main, not that the parent workstream is done. [C] points to an unmerged candidate. No [Q] release gate exists at this checkpoint.

## Dependencies and gates

Multiple workstreams run in parallel; this is **not a waterfall**.

```text
R0 reviewed baseline ──────────────────────────────────────────────────┐
R1 scientific correctness ──┬─> R6 strategy policy ──────────────────┤
                            └─> R9 research/adaptive gates           │
R2 qualified observations ──┬─> R3 OMS/ledger ──┬─> R5 broker parity ┤
                            └─> R4 risk ────────┘                   ├─> R9 release
R7 UX/installer ──────────────> operator-controlled acceptance ───────┤
R8 ops/security ───────────────> deployment evidence + external audit ┘
```

**Critical path to first defensible paper-candidate release:** freeze a reviewed candidate (R0) -> raw source identity and economically correct book/OMS (R2/R3/R4) -> actual PAPER B01–B10 evidence with controlled fault scenarios (R5) -> repeatable ops/security testing (R8) -> all seven R9 gates, with R1 and R6 calibration running in parallel. A broker-connection timeout does not imply broker history is qualified, and a green simulator run never substitutes for external acceptance.

## Incremental milestone order, with pass criteria

| Order | Milestone | Depends on | Accepted only when... |
|---|---|---|---|
| N01 | Freeze, inspect and integrate coherent candidate base | R0.1–R0.4 | Exact SHA, CI, independent code review and compatibility with main are documented; no unreviewed safety changes. |
| N02 | Read-only capture integrity | R5.1–R5.2 | Missing/contradictory account, port, currency, exact positions or history is rejected; partial source never reports COMPLETE; all source coverage flags remain false. |
| N03 | Broker-source/authentication evidence | N02, R5.3 | Independent PAPER identity and data permissions are observed from a verified session; raw evidence retained privately; reviewer signs coverage boundaries. |
| N04 | Full broker-history / OMS reconcile | N03, R3, R5.4 | No unknown order, execution, cash delta or unverifiable historical gap can silently permit a retry, release or new order. |
| N05 | Realistic multi-instrument economics and risk | R2/R4, N04 | Fractional precision, actions, corporate events, funding, borrow, impact and stressed execution reconcile against independent fixtures and supported broker evidence. |
| N06 | Scientific research quality and adaptive approval | R1, R6 | Prospective independent null/power calibration, honest trial accounting, safe strategy retirement and policy qualification complete. |
| N07 | Plug-and-play supervised PAPER product | N04–N06, R7 | Install, restore, configure, approve and start with a few operator steps; unsafe account/market/config remains refused; transparent errors and audit. |
| N08 | Operations, security and long-run trial | N07, R8 | Pinned build, real recovery and alert tests, resource/ENOSPC cases, independent high/critical security review, five market sessions. |
| N09 | Institutional release candidate | N01–N08 | All seven R9 gates are independently accepted against the **same pinned candidate/evidence**, without waiver-by-simulation. |

Note: N02/N03 is the current external-paper workstream. Additional scientific/UX/market engineering may progress in parallel **without** advancing the broker gate. The owner can opt for supervised broker evaluation sooner, but not silently turn it into unattended trading.

## External acceptance scenarios (under R5/R9)

| Scenario | Focus |
|---|---|
| B01 | Non-trading paper account/port/identity/data preflight |
| B02 | Broker smoke intent and terminal cleanup |
| B03 | Daily open, partial fills, commissions, cash and positions |
| B04 | Restart after uncertain intent and duplicate executions |
| B05 | Cancel/fill race and late executions |
| B06 | Evidence-based disposal, reappearance and wrong-account refusal |
| B07 | Simultaneous long/short reservations, margin and gap risk |
| B08 | Dedicated/shared-account cash, fees, borrow and overnight funding |
| B09 | 15-minute cycle, stale cancellation, kill/flatten and session boundaries |
| B10 | Source bars, timestamp alignment, DST/holidays/early-close |

See `docs/paper-acceptance.md` for full conditions; a normal market session cannot substitute for fault injection.

## Research-audit coverage map

| Audit finding | Roadmap node |
|---|---|
| F-R01 / F-R02 | R1.3 |
| F-R03 / F-R04 | R1.4 |
| F-R05 | R1.2 |
| F-R06 / F-R12 | R2.2 |
| F-R07 / F-R09 | R2.3 |
| F-R08 | R2.4 |
| F-R10 / F-R11 | R4.3 |
| F-R13 | R4.4 |
| F-R14 / F-R15 | R1.5 |
| F-R16 | R1.6 |
| F-R17 | R6.3 |
| F-R18 | R2.1 (event-construction performance) |
| F-R19 | R8.2 |

This mapping says **where to resolve** a finding, not that all 19 remain unfixed. Each finding's actual status must be re-evaluated against code, measurements and the independent review thread `C006` before marking the linked milestone complete.

## Current checkpoint and evidence

- Main reference at roadmap creation: `cb0ee1d67f64cdb1e684e60eaba0718ad16a6723`; mainline CI had a green result, but this is not broker certification.
- Latest B01 integrity candidate when this document was drafted: `chatgpt/b01-observation-integrity-20261027` at `888171271366fc01473585d2cc22e88ddb8b514c`. Hosted CI for this exact revision **passed** in GitHub Actions run `40501459139`; prior B01 connection-failure branch at `6f34e00994c0f0f869669807f03561e528b320b9` also had green CI. Neither run constitutes authenticated broker qualification.
- Prior actual PAPER preflight reached the local Gateway but experienced IBKR data and security-definition farm outages followed by a connection timeout. That does **not** produce a qualified B01 snapshot. Root cause remains open and requires supervised operator context.
- The seven release gates remain **blocked** in `docs/release-criteria.json`. Do not change them to green based on this roadmap or feature tests.
- No passwords, actual account identifiers, exchange API keys, secret paths, account balances or raw private evidence belong in the roadmap.

## Definition of done / updating this tree

Every milestone must carry, in a future reviewable status update:

1. **Candidate and scope:** exact Git commit, changed modules, supported markets, accounts, OSes, modes and unsupported cases.
2. **Correctness evidence:** automated deterministic tests, adverse-path/fault cases, reproducibility/data lineage, no risk-gate relaxations, paper-only/no-order boundaries as appropriate.
3. **Independent review:** reviewer identity, blocking findings answered, exact reviewed SHA, merge/proposal link.
4. **Qualification:** true external evidence where required (broker, security, long-running deployment); simulation is separately labeled.
5. **Exit decision:** [Q] only when scope-specific acceptance passes; otherwise [M], [C], [P], [T] or [E], with explicit blockers.

Keep this file as the **stable milestone taxonomy**. Record rapidly changing implementation states and evidence in named, dated updates / tickets; never alter history or prematurely rename a roadmap milestone to "done". The repository is currently a high-ambition research prototype, not a qualified institution-grade autonomous trading service.
