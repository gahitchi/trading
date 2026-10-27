# Trading Lab — hierarchical development roadmap

**As of:** 2026-10-27. **Product:** institutional-quality, reproducible, broker-neutral research platform with deliberately gated PAPER execution. **This is a dependency roadmap, not a promise of completion dates.**

## Provenance and strict status rules

- Reviewed integration baseline inspected: `main` at `cb0ee1d67f64cdb1e684e60eaba0718ad16a6723` (green CI run `40491788493`).
- Latest ChatGPT paper-capture candidate inspected: `chatgpt/b01-observation-integrity-20261027` at `888171271366fc01473585d2cc22e88ddb8b514c` (green CI run `40501459139`). This is **not** a merge into `main`.
- Mandatory release tracker: `docs/release-criteria.json`; all **seven release gates remain blocked**. Live-money order execution remains out of the qualified product scope.
- Independent paper-broker scenarios: `docs/paper-acceptance.md`, B01–B10. Current user-operated Gateway attempt did **not** produce a qualified B01 snapshot, due to broker-data readiness timeout.
- Quantitative upgrade baseline: `docs/audits/2026-10-24-quantitative-research-upgrade-audit.md`, F-R01–F-R19. The audit **is a task inventory, not evidence that every finding is open or fixed**; verify each against current code before claiming closure.

**Legend (do not interchange statuses):**
- `B` — implemented **foundation**, possibly on a feature branch or as an offline pilot; **not** independently qualified.
- `W` — active engineering/hardening/integration; some supporting code may exist.
- `P` — scoped, planned, without a qualified end-to-end implementation.
- `X` — needs external broker/exchange/operator observation or deployment evidence.
- `G` — mandatory independent signoff and release gate, regardless of test coverage.

A milestone is **DONE** only if its exact implementation SHA is integrated into `main`, reproducible tests and negative/fault tests pass, an independent reviewer accepts the evidence, documentation and migration/rollback implications are accounted for, and no blocking finding remains for its declared scope. A broker-dependent milestone additionally requires actual authenticated source evidence. A `B` or green CI job never automatically means DONE.

## Dependency tree

```text
TRADING LAB
│
├── M0  Architecture & governance
│   ├── M0.1  Broker-neutral system boundaries       [B]
│   ├── M0.2  Collaboration and code review          [B]
│   ├── M0.3  Release criteria and evidence rules    [B]
│   └── M0.4  Consolidate reviewed branches          [W]
│
├── M1  Core trading infrastructure
│   ├── M1.1  Event-driven engine                    [B]
│   ├── M1.2  Durable order/event journal           [B]
│   ├── M1.3  Exact accounting and state replay     [W]
│   ├── M1.4  Deterministic failure recovery        [W]
│   └── M1.5  Native-engine parity qualification    [P]
│
├── M2  Market data & execution realism
│   ├── M2.1  Historical market-data ingestion       [B]
│   ├── M2.2  Source authenticity and gap checks    [W]
│   ├── M2.3  Corporate actions and point-in-time   [W]
│   ├── M2.4  Order book, latency and fill models   [W]
│   └── M2.5  Fees, funding, borrow and FX          [W]
│
├── M3  Quantitative research engine
│   ├── M3.1  AI hypothesis and strategy pipeline   [B]
│   ├── M3.2  Sandboxed strategy validation         [B]
│   ├── M3.3  Robust statistical evaluation         [W]
│   ├── M3.4  Walk-forward and leakage controls     [W]
│   ├── M3.5  False-positive/power calibration      [P]
│   └── M3.6  Independently qualified strategies    [G]
│
├── M4  Portfolio construction & risk
│   ├── M4.1  Configurable risk profiles             [B]
│   ├── M4.2  Exposure and allocation engine        [B]
│   ├── M4.3  Pending-order worst-case risk         [W]
│   ├── M4.4  Margin and financing constraints      [W]
│   └── M4.5  Portfolio-level risk qualification    [G]
│
├── M5  Orders, brokers & reconciliation
│   ├── M5.1  Order lifecycle and idempotency       [B]
│   ├── M5.2  Cancel/fill/restart fault handling    [W]
│   ├── M5.3  Independent broker reconciliation     [W]
│   ├── M5.4  Authenticated evidence and history    [X]
│   └── M5.5  Complete broker acceptance B01–B10    [X]
│
├── M6  Multi-market integration
│   ├── M6.1  IBKR equities PAPER adapter           [W]
│   ├── M6.2  Gold PAPER integration                [W]
│   ├── M6.3  Crypto public feeds / offline engine  [B]
│   ├── M6.4  Crypto demo broker integration        [P]
│   ├── M6.5  Forex integration                     [P]
│   └── M6.6  Unified multi-market portfolio        [P]
│
├── M7  Application and operator experience
│   ├── M7.1  Arch and Windows installers           [W]
│   ├── M7.2  Guided initial configuration          [W]
│   ├── M7.3  Dashboard and diagnostics             [W]
│   ├── M7.4  Start/stop and authority workflow     [W]
│   ├── M7.5  Natural-language trade configuration  [P]
│   └── M7.6  Recovery and notification UX          [P]
│
└── M8  Validation and release
    ├── M8.1  Automated tests and CI                [B]
    ├── M8.2  Independent quantitative audit        [W]
    ├── M8.3  Security and deployment audit         [G]
    ├── M8.4  Real paper-broker acceptance          [X]
    ├── M8.5  Long-duration reliability testing     [X]
    ├── M8.6  Seven mandatory release gates         [G]
    └── M8.7  Qualified PAPER release               [P]
```

## Concrete verification contracts for each epic

| Epic | Smaller deliverables and acceptance evidence |
|---|---|
| **M0** | Specify instrument/account/scope interfaces with typed ownership; enforce branch and immutable review-message protocol; keep machine-readable release gates; integrate only reviewed SHAs with green CI and traceable migration. No safety-critical change reaches the release candidate unreviewed. |
| **M1** | Establish replayable source -> intent -> acknowledgment -> partial fill -> cancel -> settlement transitions; prove exact-Decimal cash and position conservation, deterministic state-digest equality after restart, no duplicated execution booking, and Nautilus/account/ledger parity for a declared scenario. |
| **M2** | Store immutable source timestamps and revisions; reject gaps/out-of-order feeds and wrong session boundaries; prove historical point-in-time membership/corporate actions; add independently benchmarked fills under depth, latency, spread/impact and stress; reconcile historical and actual funding, short borrow, commissions and FX per currency. Audit scope: F-R06–F-R09, F-R12 and F-R18. |
| **M3** | Keep AI proposals isolated from approvals and holdout; test serial-correlation-consistent PSR/DSR, correlated trial counts, purged/embargoed CV, stationary bootstrap, session-end excess returns, factor tear sheets and active-window walk-forward; run prospectively frozen null/power calibration without threshold tuning after observing results. Audit scope: F-R01–F-R05, F-R14–F-R16. |
| **M4** | Prove configured risk presets change real numeric budgets; cap portfolios across simultaneous orders, partial-fill subsets and adverse marks; account for buy/sell, long/short, margin tiers, currency and borrowing constraints; prove halts/recovery and supervised/automatic authority cannot bypass broker permission or risk approval. Audit scope: F-R10–F-R13 and F-R17. |
| **M5** | Prove every intent and execution has stable identity and causal provenance; quarantine unknown/cancel-pending orders; reconcile broker account, complete order/fill cursor history, cash/financing/fee ledger and recovery state. Accept B01–B10 only with private actual PAPER evidence and independent reviewer decisions; never derive authentication from a local file hash. |
| **M6** | Qualify each adapter independently (instruments, clock/session semantics, native accounting, fees and margin, broker lifecycle, recovery, permissions); preserve separation of offline crypto pilots from real venue DEMO and of PAPER Gold/equities from future FX. A multi-venue portfolio is last, after its component venues are qualified. |
| **M7** | Test clean installs/upgrades on Arch/Windows, safe broker onboarding and paper-only defaults; expose the exact lifecycle/authority/kill/market-data state in UI and notifications. A future natural-language text box may output a reviewable deterministic **configuration draft**, never send an order or grant authority directly. |
| **M8** | Verify pinned dependency/runtime/container builds on supported architectures, deterministic fast and real-clock tests, attack/resource-exhaustion tests, physical deployment recovery and long-duration soak. Run `tools/release_check.py` against the exact candidate; independent reviews must satisfy all seven release gates before a PAPER release is labeled qualified. |

## Highest-resolution near-term work breakdown

The immediate **critical-path** tasks are intentionally smaller than the major milestones. Complete them in dependency order, marking a result only when evidence exists:

```text
A. Merge discipline (M0.4)
   A1  Review latest branch diffs and outbox findings
   A2  Resolve every blocking review item by ID
   A3  Integrate exact reviewed SHAs into main
   A4  Re-run the full current-main CI + migration checks
   Exit: documented integrated SHA, independent approval and green CI

B. B01 evidence hygiene (M5.3 -> M5.4)
   B1  Exact numeric Money/Quantity export              [code candidate]
   B2  Incomplete/complete snapshot commit marker        [code candidate]
   B3  Account, port, position, timestamp consistency    [code candidate]
   B4  Read-only private capture on actual PAPER Gateway [external]
   B5  Verify authenticated broker-session identity     [not qualified]
   B6  Document data farm/contract readiness and limits  [external]
   B7  Determine complete order/execution history model  [not qualified]
   Exit: witnessed broker-origin B01 evidence; no order submission

C. Independent OMS and finance (M1.3/M1.4/M5.2/M5.3)
   C1  Exactly-once partial-fill replay and crash tests
   C2  Cancel-vs-fill, unknown-order and late-fill traces
   C3  Double-entry cash, fee, financing and currency proof
   C4  Native engine vs independent ledger parity
   C5  Full snapshot/cursor reconciliation and safe restart
   Exit: deterministic negative and recovery tests, reviewer-approved

D. Quantitative qualification (M2/M3/M4)
   D1  Re-audit F-R01–F-R19 against exact source and tests
   D2  Implement/review highest-risk corrections independently
   D3  Point-in-time market data/borrow/corporate-action fixtures
   D4  Freeze full strategy-selection null/power test protocol
   D5  Run adequate independent calibration + risk stress
   Exit: prospective error/power evidence with no gate relaxation

E. Product and deployment (M6/M7/M8)
   E1  Separate qualified PAPER and offline DEMO workflows
   E2  Installer clean-machine/upgrade/rollback acceptance
   E3  Clear UI authority, position, evidence and halt states
   E4  External B02–B10 acceptance after B01
   E5  Security review, capacity/fault tests, five-session soak
   E6  Seven release gates + independent final signoff
   Exit: exact candidate has complete reviewer-attested evidence
```

**Parallelizable now:** A1–A3, B1–B3, C1–C4, D1–D4, E1–E3. **Needs owner/external resources:** B4–B7, actual B02–B10, broker-derived funding/fees, clean-machine acceptance and real-soak runs. Termux supports standard-library offline probes but **cannot** certify the native full NautilusTrader runtime or an authenticated broker session.

## Release dependency graph

```text
    REVIEWED MAIN / REPRODUCIBLE BUILD (M0 + M8.1)
                    |
      +-------------+--------------+------------------+
      |             |              |                  |
  DATA PROOF      RESEARCH      OMS + RISK        INSTALL / UX
   M2.2–2.5       M3 + M4         M1 + M5          M7
      |             |              |                  |
      +-------------+--------------+------------------+
                    |
            READ-ONLY B01 (M5.4)
                    |
         BROKER LIFECYCLE B02–B10
                    |
          INDEPENDENT SEVEN GATES
                    |
           QUALIFIED PAPER RELEASE
                    |
          [Separate future decision:
        additional markets / live money]
```

## Definition of done and recurring status reporting

For each submilestone, track: **ID, named owner, branch/SHA, baseline assumption, positive tests, negative/fault tests, external evidence if required, blocking findings, reviewer, integration SHA, dependency status**. If any required evidence is absent, report `blocked` or `in development` rather than estimated completion percentages.

At each development checkpoint, report: most recent green integrated `main`; active candidate with exact CI status; submilestones newly implemented/qualified; unresolved blockers and failing tests; next work without external input; explicit operator-dependent action, if any.

**Non-goals for this roadmap:** guessing profitability, treating a synthetic strategy as qualified, asserting broker completeness from cache data, enabling live money, or masking blocked release gates behind UI polish.
