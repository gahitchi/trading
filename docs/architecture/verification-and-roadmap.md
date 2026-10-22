# Requirements traceability, staged implementation and acceptance

**Current verdict: NOT CLEARED for unattended paper execution.**
Passing Python unit tests and Docker builds is necessary but not sufficient.

## 1. Implementation order (dependency-aware)

| Phase | Scope | Required proof |
| --- | --- | --- |
| P0-A: contracts | Stable event IDs, exchange session model, immutable roster, completeness barrier, typed risk-permit model | Deterministic simulations including holidays, duplicate/out-of-order events, mixed data |
| P0-B: storage | SQLite WAL schema, durable single writer, ledger/outbox/inbox, identity-bound migration, backup/restore | Crash before and after each transaction, duplicate fills, independent accounting reconstruction |
| P0-C: broker | Paper-only broker identity, full account/order/fill/cash reconciliation, separate broker liveness, source coverage proof | IBKR DU integration with reconnect, unknown orders, foreign holdings, fee changes, partial fills |
| P0-D: controls | Independent risk monitoring, atomic risk permit/order reservation, halt/kill/recovery, realistic cash | No submit after halt; no buying power overbooking; no blind retry; unknown state blocks |
| P1-A: research | Immutable point-in-time data/provenance, unbiased simulations, frozen holdout, statistical validation | Reproducible experiments, no lookahead, joint null tests and complete trial registry |
| P1-B: AI boundary | Isolated workers, immutable promotion manifest, verified artifact load | Adversarial sandbox attempts, tampering and dependency change rejected |
| P1-C: operations | Artifact signing/locks, secure packaging, NYSE calendar, alerting, backup/restore, runbooks | End-to-end shadow + supervised paper gates, operator drills |
| P2: extensions | Multiple asset classes, sophisticated portfolio/risk methods, optimization at scale | Separate design and broker-specific qualification; not required for first safe US-stock paper deployment |

**Do not optimize model sophistication before P0.** The quality of trading research is irrelevant if order state can double-submit or balances cannot be reconstructed.

## 2. Detailed test matrix

These test IDs are acceptance criteria to implement, **not** tests claimed to already exist.

| Test | Contract | Example injected failure | Passing behavior | Current |
| --- | --- | --- | --- | --- |
| CT-01 | INV-01 | Broker reports live or wrong DU account | Zero submit attempts; critical alert | Partial |
| CT-02 | INV-02 | One missing daily stock bar | No sealed snapshot; no orders | Missing |
| CT-03 | INV-02 | Friday bar replayed on Monday | No Monday decision | Missing |
| CT-04 | INV-02 | Mixed two exchange-session dates | Reject incomplete snapshot | Missing |
| CT-05 | INV-02 | Early close / DST / NY holiday | Correct deadline or no scheduled run | Missing |
| CT-06 | INV-03 | Risk approval precedes last signal | No order until new permit | Missing |
| CT-07 | INV-03 | Approval reused against changed equity | Reject expired/mismatched approval | Missing |
| CT-08 | INV-04 | Lost submit ACK after broker accepted | No duplicate on restart; UNKNOWN and broker query | Missing |
| CT-09 | INV-04 | Broker disconnect during cancellation | Remain pending, no blind retry/assumed terminal | Missing |
| CT-10 | INV-05 | Replay same execution ID twice | Exactly one cash/position/fee entry | Partial |
| CT-11 | INV-05 | Partial fill 30/100 and cancel remainder | 30-share inventory, balanced cash/reservation | Partial |
| CT-12 | INV-05 | Late commission correction / corporate action | Ledger and broker converge with separate journal entry | Missing |
| CT-13 | INV-06 | Restart mid-session after daily limits reached | Caps not reset | Partial |
| CT-14 | INV-06 | Risk state lost while ledger exists | Trading refused until reviewed recovery | Missing |
| CT-15 | INV-06 | Equity drawdown between rebalance ticks | Independently halt as configured | Missing |
| CT-16 | INV-07 | Disk full on intent commit or fill booking | Never send unlogged order or corrupt money state | Partial |
| CT-17 | INV-08 | Kill issued without any new price bar | Immediate submission halt and cancel workflow | Partial |
| CT-18 | INV-08 | Flatten requested while exchange closed/halted | Never claim completion; retain halt/escalate | Missing |
| CT-19 | INV-09 | Research artifact modified after promotion | Digest mismatch, cannot be loaded | Missing |
| CT-20 | INV-09 | Malicious signal probes network/secret mounts | OS sandbox denies | Missing |
| CT-21 | INV-10 | Corporate action known only in future | Backtest refuses future-informed prices | Missing |
| CT-22 | INV-10 | Data ends early with no validated delisting event | No inferred prescient liquidation | Partial |
| CT-23 | INV-10 | Change one bar in experiment input | Dataset fingerprint changes and results invalidated | Missing |
| CT-24 | INV-11 | Two deployed copies of same DU strategy | Exactly one can own dispatcher lease | Missing |
| CT-25 | INV-12 | Heartbeat recent, broker account stale | Liveness green, trading readiness red | Partial |
| CT-26 | INV-13 | Set live port/account in config | Deployment and runtime both refuse | Partial |
| CT-27 | INV-14 | Restore DB before last IBKR fills | Resume denied until execution-history reconciliation | Missing |
| CT-28 | INV-14 | Roll back runtime version across schema upgrade | Safe rollback or explicitly abort with protected data | Missing |
| CT-29 | INV-07 | Replay all cash/position/audit events | Materialized balances match journal and IB statement | Missing |
| CT-30 | INV-02 | Updated conflicting signal after snapshot seal | No second order; incident record | Missing |

"Partial" means **some related unit check/code exists**, not that the contract is satisfied.

## 3. CI and validation stages

- **Fast PR gate**: format/type/static checks, pure unit tests (Python versions supported by pinned NautilusTrader), deterministic randomized property tests, schema migrations, no generated output drift.
- **Broker-simulator gate**: replay delayed/duplicated/reordered broker events, balance invariants, limit checks, full crash-point matrix, trading calendar fixtures and snapshot/risk separation. Must be network-independent.
- **Security gate**: dependency SBOM, vulnerability and secret scans, provenance/lockfiles, malicious signal and privilege escalation tests.
- **Staging gateway gate**: supported NautilusTrader/IBKR paper version, explicit test DU account, complete account-state queries, outbox/ACK/partial-fill/cancel/restart integration in supervised conditions. No test relying on "a limit order at half the last price cannot fill"; any valid live order may fill.
- **Shadow soak**: full live-data chain, submissions disabled, capture no-trade/recovery/holiday cases, verify signal and portfolio reproducibility.
- **Supervised paper soak**: bounded capital/order limits, operator attending deployment/session and injected outages, reconciled fills and auditable daily reports.
- **Unattended paper gate**: formal review of all INV/CT IDs, no P0 failures, recovery drills, alert channel delivery, broker and exchange-specific operating constraints, actual reconciled trade history. No automatic gate waiver.

## 4. Definition of done for each small change

1. Exact behavioral requirement and out-of-scope statement.
2. Typed versioned inputs/outputs and migrations.
3. State machine plus concurrency/idempotency rules.
4. Failure, restart and rollback procedure.
5. Deterministic tests: normal, negative, boundary, duplicate, clock/data corruption and injected broker exceptions.
6. Proof of no forbidden live-account path and no unintentionally broadened permissions.
7. Observability and audit, including who gets paged.
8. CI evidence, broker/simulated integration evidence where applicable, review approval and documentation updates.

## 5. Next small design slices

**Slice 1:** typed `SessionKey`, exchange calendar adapter, immutable `ExpectedRoster` and `MarketBarFinalized` with fixtures and checks, *without* changing live behavior.

**Slice 2:** `SessionSnapshotBuilder`: collect exact roster, validate completeness, reject duplicates/conflicts, seal one immutable snapshot; unit tests across dates/late bars.

**Slice 3:** strategy evaluation over sealed snapshot, atomic portfolio proposal including explicit zero positions, pure risk-policy evaluation binding a permit ID.

**Slice 4:** broker order/ledger schema and isolated deterministic simulated broker; no real broker connectivity in the unit-test harness.

Only after these small designs and their tests are correct should we wire the session barrier into `paper.py` and the live `ExecutionAgent`. This avoids applying an ad-hoc patch to the wrong abstraction layer.
