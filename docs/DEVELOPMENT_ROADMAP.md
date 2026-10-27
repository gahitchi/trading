# Trading Lab — foundation-first execution roadmap and milestone taxonomy

**Revision:** 2026-10-27 · **Status:** proposed, not yet merged into `main` · **Baseline:** `cb0ee1d67f64cdb1e684e60eaba0718ad16a6723`

> **Authority rule:** F0–F8 are the strict dependency-ordered *qualification gates*. The R-tree is the *work breakdown* nested inside those gates, **not** permission to develop or certify in arbitrary numeric order. Every phase's exit requires evidence that earlier foundation gates remain valid on the **same exact candidate revision**. Capability growth may invalidate previously qualified evidence. Planned functionality may be prototyped early in an isolated branch, but never admitted to the qualified path before its prerequisites pass.

## 1. Two distinct views — never conflate them

**Gate status**: `BLOCKED` (not yet qualified), `CANDIDATE` (implementation and tests exist; acceptance pending), `QUALIFIED` (reviewed, exact-SHA evidence satisfying all exit conditions), or `INVALIDATED` (scope/code/data/dependency change requires requalification). An implemented algorithm, passing CI, synthetic paper run or a green dashboard does **not** change gate status.

**Feature state**: `PRESENT` (code on main, not necessarily qualified), `BRANCH` (candidate outside main), `PARTIAL`, `PLANNED`, `EXTERNAL` (awaiting real broker/operator/independent evidence). Feature-state and gate-status are separate fields.

**First vertical slice:** one selected equity/ETF on one explicitly PAPER IBKR gateway, one restricted daily strategy policy, USD account and one known exchange calendar. The chosen symbol, corporate-action history, rules, broker data permissions and economic assumptions must be pinned before formal qualification. This is an **internal reference slice**, not a declaration that user-requested multi-asset paper/demo v1.0 is complete. Later expansion must eventually support stocks/ETFs, crypto and forex/demo separately, with fresh evidence per market. Gold remains its own scoped optional track.

**Excluded from the core-reference candidate:** additional brokers, exchange-grade crypto execution, forex funding, new Gold qualification, regime-swarm optimization, natural-language order submission, unsupported margin/short modes and live-money trading. Experimental modules can stay available behind explicit unqualified boundaries but cannot inherit the core candidate's approval.

## 2. Strict causal phase gatekeeper (F0–F8)

```text
F0  REPRODUCIBLE BASELINE                     [prerequisite of every gate]
 │  immutable candidate / pinned deps / CI / threat and failure-test harness
 ▼
F1  ECONOMIC CONSERVATION                    [single equity/ETF, USD]
 │  ledger ↔ engine: fills, fees, splits, cash, settlement, conservation
 ▼
F2  POINT-IN-TIME INPUTS                     [qualified source contract]
 │  revision-aware prices, membership, actions, sessions, availability times
 ▼
F3  TYPED DECISION + RISK CONTRACT           [single source of authority]
 │  reduce-only, pretrade reserves, exact limits, kill, unknown-state refusal
 ▼
F4  ORDER STATE MACHINE + RECOVERY           [crash and concurrency proof]
 │  durable journal, idempotent fills, cancel races, replay, reconciliation
 ▼
F5  SCIENTIFIC MODEL QUALIFICATION           [no optimistic simulated edge]
 │  costs, purging, holdout, multiple testing, null/power calibration
 ▼
F6  AUTHENTICATED SUPERVISED PAPER BROKER    [first external authority check]
 │  B01–B10 actual broker evidence, reconciliation, clocks, fees, recovery
 ▼
F7  OPERATOR HARDENING + DEPLOYMENT          [supported OS and operation]
 │  installer/UI/authority, rollback, storage, alarms, soak, external security
 ▼
F8  INDEPENDENT SCOPE-LIMITED RELEASE GATES  [all seven, same candidate SHA]
    code review / lifecycle / calibration / data+execution / adaptive /
    operations+build / independent security

POST-FOUNDATION SCOPE EXPANSION — each additional market or capability:
    bounded design + sandbox tests → repeat affected F1–F7 → all seven F8 gates
```

**No shortcut:** F6 broker observation cannot compensate for broken F1 accounting; F5 statistical significance cannot compensate for F2 source leakage; F7 installer polish cannot compensate for unsafe F3/F4 execution. F0 reproducibility and security hygiene remain continuously enforced at every stage.

## 3. Phase-by-phase milestone tree (chronological dependencies)

```text
R0 / F0  GOVERNANCE AND REPRODUCIBLE BASELINE
  R0.1  [PRESENT] Freeze scoped first vertical slice, supported modes and exclusions
  R0.2  [PRESENT] Deterministic build: pinned Python, native libs, Docker, hashes
  R0.3  [PRESENT] CI on every change: lint, format, fast tests, serial real-clock harness
  R0.4  [PARTIAL] Immutable acceptance manifest with source/config/data/OS identity
  R0.5  [PARTIAL] Document architecture contracts, attack surface, owners, decisions
  R0.6  [PARTIAL] Independent code review and explicit blocking-finding resolution
  EXIT: reproduce exact candidate from pinned inputs; all required CI/security
        baseline tests pass; reviewable hashes and build provenance recorded.
        Re-apply F0 to EVERY later change.

R1 / F1–F2  DATA CUSTODY AND ECONOMIC CONSERVATION
  R1.1 / F1  [PARTIAL] Independent immutable double-entry engine/ledger parity
  R1.2 / F1  [PARTIAL] Exact position, cash, fee, financing, settlement lifecycle
  R1.3 / F1  [PARTIAL] Buy/sell/partial-fill/reversal/late-fill conservation proofs
  R1.4 / F1  [PARTIAL] Corporate-action/split/dividend and marked-price invariants
  R1.5 / F2  [PARTIAL] Point-in-time source, membership, data revision, as-of/available
  R1.6 / F2  [PARTIAL] Exchange calendar, closed-bar fence, timestamp and delay truth
  R1.7 / F2  [PARTIAL] Realistic but explicitly scoped fees, impact, volume and prices
  R1.8 / F2  [PLANNED] Source outage, revision, survivorship, stale-data adversarial tests
  EXIT: a penny/share discrepancy, missing economic leg or future/revised input
        blocks qualification; reconciliation reproduces with raw source evidence.
        No qualified strategy metrics before R1 exit.

R2 / F3–F4  TYPED RISK CONTRACT AND ORDER STATE MACHINE
  R2.1 / F3  [PARTIAL] Single typed TradeDecision / OrderIntent risk-authority boundary
  R2.2 / F3  [PARTIAL] Reduce-only, kill/flatten, cash, gross/net/short/margin invariants
  R2.3 / F3  [PARTIAL] Reservations across every fill subset; stressed price/fee gaps
  R2.4 / F3  [PARTIAL] Quarantine mismatched, unknown or unauthorized broker state
  R2.5 / F4  [PRESENT] Journaled identity, idempotent fills and durable order lifecycle
  R2.6 / F4  [PARTIAL] Pending-cancel/fill races, duplicates, out-of-order and restarts
  R2.7 / F4  [PARTIAL] Native engine, ledger, OMS and reports agree after each transition
  R2.8 / F4  [PARTIAL] Failure injection at every persistence/network boundary
  EXIT: deterministic replay and injected crashes cannot double-book fills,
        release uncertain reservations, silently resubmit or exceed risk budgets.
        An unresolved broker fact must fail closed.

R3 / F5  SCIENTIFIC RESEARCH AND STRATEGY QUALIFICATION
  R3.1  [PRESENT] Restricted strategy sandbox, code/data digests, holdout ledger
  R3.2  [PARTIAL] Tradeable/economic excess returns, financing, active windows
  R3.3  [PARTIAL] Autocorrelation-HAC PSR/DSR and correlated-trial correction
  R3.4  [PARTIAL] Purged/embargoed walk-forward and bootstrap with error controls
  R3.5  [PARTIAL] Point-in-time cross-section and optimizer covariance/shrinkage
  R3.6  [PARTIAL] Trial-family accounting, robust null, false-positive and power study
  R3.7  [PARTIAL] Exact candidate approval, bounded fallback and strategy eligibility
  EXIT: prospective protocol, held-out and correlated-panel null/power evidence
        pass predefined statistical bounds on F1–F4-qualified data and economics.
        No publication or strategy activation based on test-data leakage.

R4 / F6  SINGLE-VENUE AUTHENTICATED SUPERVISED BROKER QUALIFICATION
  R4.1  [BRANCH] Offline B01 read-only capture, provenance, exact balances,
        incomplete-state quarantine; candidate in chatgpt/b01-observation-integrity-20261027
  R4.2  [EXTERNAL] Authenticated IBKR PAPER account and data-farm readiness
  R4.3  [EXTERNAL] Complete account/order/execution/position broker history,
        availability clocks and cursors; no cache=completeness shortcut
  R4.4  [EXTERNAL] Independent ledger vs broker cash/fees/funding reconciliation
  R4.5  [PARTIAL] Broker-neutral adapter contract plus reconnect/fail-closed behavior
  R4.6  [EXTERNAL] Supervised B01–B10 (account; smoke; fills; crash; cancel;
        disposal; margin; financing; intraday; raw bars) and anomaly review
  EXIT: independently authenticated, immutable raw-source and reviewed
        sanitized evidence prove all B01–B10 applicable conditions; unknown
        history or financial discrepancy blocks submission/recovery.
        Live-money permission is NOT implied.

R5 / F7  OPERATOR WORKFLOW, DEPLOYMENT, HARDENING
  R5.0  [PARTIAL] EARLY SAFETY DEPENDENCY: minimal paper identity/port check,
        risk dashboard, kill/flatten visibility, logs and status BEFORE R4 trials
  R5.1  [PARTIAL] Arch and Windows launcher/installer, pinned upgrade/rollback
  R5.2  [PARTIAL] Single guided PAPER configuration and authority confirmation
  R5.3  [PARTIAL] Read-only observations of actual orders, fills, cash, risk and halts
  R5.4  [PARTIAL] Cold backup, recovery, migration, alerts and incident audit
  R5.5  [PLANNED] Storage exhaustion, power loss, restore and unbounded-data growth
  R5.6  [EXTERNAL] Five supervised US market sessions with validated recovery
  R5.7  [EXTERNAL] Independent security assessment; supported environments
  EXIT: repeatable install, safe operations, clean rollback, bounded failure,
        overnight/session conditions and no unresolved critical/high security
        finding. F0 and F3/F4 unit/crash/security tests have been running all along;
        R5 is not the first appearance of CI, resilience or operator visibility.

R6 / F8  FORMAL CORE-SLICE ACCEPTANCE — NOT YET MULTI-MARKET v1.0
  R6.1  [EXTERNAL] code-review: exact SHA, blocking findings and required CI
  R6.2  [EXTERNAL] broker-lifecycle: B01–B09 with independent real PAPER evidence
  R6.3  [EXTERNAL] research-calibration: prospective selection/power/null criteria
  R6.4  [EXTERNAL] market-data-and-execution: B07–B10 + economic source truth
  R6.5  [EXTERNAL] adaptive-product: qualified bounded manual/supervised/autonomous
        eligibility, expiry, retirement and safe fallback for *supported scope*
  R6.6  [EXTERNAL] operations-and-build: recovered failures, pinned images/OS,
        alerts, resource soak and historical evidence
  R6.7  [EXTERNAL] independent-security: critical/high review cleared
  EXIT: all seven entries of docs/release-criteria.json accepted for the SAME
        exact candidate by independent reviewers; no threshold exemptions.
        The present adaptive-product requirement stays BLOCKED unless its
        core bounded policy is actually qualified; deferring advanced AI does
        not waive it.

R7 / POST-F8  INCREMENTAL EXPANSION (NOT PART OF INITIAL CORE ACCEPTANCE)
  R7.1  [PLANNED] Segregated crypto demo: fees, funding, precision, calendar,
        exchange data and book/account settlement; independently requalify
  R7.2  [PLANNED] Segregated forex demo: rollover/swap, FX conversion,
        venue/contract settlement; independently requalify (former D5)
  R7.3  [PARTIAL] London Gold/commodity PAPER feature: scope and specialized
        broker settlement qualification; not inherited from the stock proof
  R7.4  [PLANNED] Additional brokers: adapter conformance and paper history,
        no privilege inheritance from IBKR
  R7.5  [PLANNED] Regime switching, strategy-bank adaptation and optimization
        requiring new prospective calibration and safe rollback
  R7.6  [PLANNED] Natural language -> non-executable typed proposal -> human
        confirmation -> F3 risk checks; NEVER direct LLM->order dispatch
  R7.7  [PLANNED] One-click mode only when all operator, broker, policy
        and explicit authorization conditions are already established
  EXIT PER FEATURE/MARKET: updated F0 manifest; re-run affected F1–F7; pass
        ALL SEVEN F8 gates for the expanded exact candidate. Only after stocks,
        crypto and forex are each supported and requalified may the full
        planned **multi-market paper/demo v1.0** be described as qualified.
        Real-money trading is a separate authorization and release.
```

## 4. Parallel engineering vs non-negotiable qualification dependencies

This is **not** a prohibition on concurrent coding. It is a prohibition on advancing a *qualification claim* out of order.

- **Always from day one:** F0 builds/CI, baseline security/secret isolation, replay harness, deterministic simulations, risk-gate tests and basic operator kill/status visibility. These are not deferred to R5.
- **R1/R2 parallel work:** data ingestion and economic accounting may be implemented concurrently; R1's full exit is prerequisite to R2's qualified claims. Risk invariants and crash tests must run throughout.
- **R3 research prototypes:** possible in isolation while earlier foundations are under construction, but cannot pass the F5 strategy approval or promote to broker execution before F1–F4.
- **R4 paper preflight:** read-only diagnostics may be developed earlier; qualification, test orders or permission to dispatch belong only after the corresponding preceding gates and a supervised operator plan. A local TCP connection or unverified cache is not source authentication.
- **R5 UX/ops:** minimum safe monitoring and kill/authority visibility precede external R4 trials; F7 certifies completed installers, failover, storage and support, not first implementation of those protections.
- **R7 expansion:** market adapters/AI prototypes may exist in separate branches but are excluded from the reference qualified candidate. Cross-market integrations must not silently make old research approvals reusable.

## 5. Engineering acceptance ledger / honest checkpoint (2026-10-27)

| Foundation gate | State at roadmap revision | Principal outstanding evidence |
|---|---|---|
| F0 | Implemented infrastructure, **gate not independently certified for release** | Pin exact unified candidate and reconcile reviewed features / OS provenance. |
| F1 | Partial | Full native/account-vs-ledger conservation for all supported events, margin, settlement, fees. |
| F2 | Partial | Point-in-time source/revisions, corporate actions, calendars and timestamp availability. |
| F3 | Partial | Typed risk contract, every-fill-subset stress/reservation proofs, unknown-state quarantine. |
| F4 | Partial | Native engine/OMS parity, injected restart/cancel/late-fill and no-double-book proofs. |
| F5 | Partial | External predeclared null/power and complete economic/holdout qualification. |
| F6 | Blocked external | Genuine IBKR PAPER session/farm evidence and B01–B10, financial/history parity. |
| F7 | Partial and external | Operator deployment, restore/soak, alerts and independent security. |
| F8 | **Blocked, seven of seven** | All exact-SHA review and release evidence gates in `docs/release-criteria.json`. |

`main` at creation: `cb0ee1d67f64cdb1e684e60eaba0718ad16a6723`. Existing B01 read-only integrity candidate `888171271366fc01473585d2cc22e88ddb8b514c` in `chatgpt/b01-observation-integrity-20261027` passed hosted CI run `40501459139`, but is not merged or qualified as authenticated custody. Previous actual Gateway PAPER preflight reported broker-data/security-definition farm failures and a connection timeout: no completed B01 broker qualification resulted.

Older descriptions of the R0–R9 taxonomy are **feature-inventory references only** and do not supersede this F0–F8 causal sequence. This file is the proposed canonical execution guide pending owner and independent reviewer agreement.

## 6. Mapping to existing source-of-truth criteria

| Fixed source/requirement | Phase and node |
|---|---|
| `docs/release-criteria.json` seven mandatory gates | R6.1–R6.7 at F8, *unchanged* |
| `docs/paper-acceptance.md` B01–B10 | R4.1–R4.6 and R6.2/R6.4 |
| Quantitative audit F-R01–R05 | R3.2–R3.4 (F5), dependent on R1 |
| Quantitative audit F-R06–R09 and R12 | R1.2–R1.7, R2.3 |
| Quantitative audit F-R10–R11 | R3.5 |
| Quantitative audit F-R13 | R2.3 |
| Quantitative audit F-R14–R15 | R3.4–R3.6 |
| Quantitative audit F-R16 | R0.4 and R3.1 |
| Quantitative audit F-R17 | R7.5, or minimal safely qualified R6.5 |
| Quantitative audit F-R18 | R1.5–R1.7 (throughput; non-blocking unless real-time constraints fail) |
| Quantitative audit F-R19 | R0.3 continuous CI tiering |
| Deferred multi-asset D5 / extra broker adapters | R7.1–R7.4 after reference F8 |

**No automatic completion:** Mapping an audit finding to a node is not a claim it is open or closed. Its actual status must be checked against current code and independent C006 review. This document never changes the release criteria, risk budgets, statistical thresholds, broker permissions or source-verification flags.

## 7. Change protocol for each milestone

A promotion from implementation to `QUALIFIED` requires: **(1)** exact source/build/data/config candidate identity, **(2)** raw and transformed evidence hashes plus reproducible test and failure injection, **(3)** economic/statistical/causal evidence as appropriate, **(4)** independent code/security review with addressed blockers, **(5)** the applicable exit test executed without future information, **(6)** confirmation that no upstream gate was invalidated. A change to accounting, data or execution assumptions re-opens dependent approvals and broker/source evidence; a code-only version bump does not inherit old qualification automatically.

**Current next actions (while broker is unavailable):** R0.4–R0.6 reconcile reviewable candidate and architecture identity; R1.1–R1.4 prove complete F1 economic conservation and settlement under native fills; then R1.5–R1.8 point-in-time/data provenance and R2 fail-closed risk/state tests. Continue offline scientific infrastructure only as unqualified experimental work. Do not block engine development on a Gateway outage, and do not claim the B01 observation bridge is authenticated.
