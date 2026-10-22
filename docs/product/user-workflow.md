# User workflow and product experience — v0.1

**Status: product design, not implemented UI.**
**Audience:** one technically capable operator/researcher running autonomous research and, after qualification, unattended **paper-only** trading.
**Design goal:** a single trustworthy workspace that answers four questions at any moment: **what is the system doing, what evidence supports it, what needs my decision, and what happens if I do nothing?**

The user should not have to interpret raw broker logs to determine whether the system is safe. The interface must never imply that a recent heartbeat, high backtest return, or green CI run authorizes unattended order submission.

## 1. Product structure and interaction strategy

Start with a **local-first CLI** and machine-readable JSON output backed by a single read model. Add a local authenticated web dashboard once the underlying state/authorization APIs are tested. Mobile is a notification and limited acknowledgement surface; do **not** expose broker credentials or default order submission remotely.

Top-level navigation (UI sections, not existing commands):

| Area | Primary user task | Key evidence |
| --- | --- | --- |
| **Overview** | Know whether research/trading is running safely and what needs attention | system mode, research activity, broker connectivity, data session, incidents, pending decisions |
| **Research** | Create, schedule, inspect and compare AI/human hypotheses | hypothesis rationale, experiments, failed attempts, data/code versions, compute budget |
| **Data** | Validate and version historical and live session data | provenance, completeness, market calendar, adjusted/raw, point-in-time constituent coverage |
| **Market Intelligence** | Compare market breadth, rolling risk, dependence and historical regimes | as-of date, source/data hashes, volatility, downside/tails, covariance, uncertainty |
| **Strategies** | Review approved/rejected versions and evidence | sealed strategy hash, holdout access history, uncertainty, risk/cost sensitivity |
| **Portfolio** | Simulate proposed allocations and preview risk | managed holdings, targets, cash reservations, estimated fees, proposed order plan, constraints |
| **Paper Operations** | Observe current paper run and broker reconciliation | broker DU identity, orders/fills, confirmed balances, per-session decision, alerts |
| **Incidents** | Halt, reconcile, acknowledge and resolve a fault | priority, causal chain, timeline, state machine, operator actions, completion proof |
| **Settings & Audit** | Configure explicit scopes, alerts, backups and permissions | approvals, policy versions, immutable event history, deployment version and restore evidence |

### Modes, visibly distinct

- **RESEARCH**: no broker connection and no order-sending permission; schedule experiments automatically.
- **SHADOW**: uses finalized paper/live market data to produce would-be orders, but execution adapter is physically disabled.
- **SUPERVISED_PAPER**: requires human approval of deployment, broker/account and strategy version; paper orders only, with explicit supervisor/incident runbook.
- **UNATTENDED_PAPER**: enabled **only after** the release qualification gate and broker recovery tests pass; cannot override critical halts.
- **HALTED / RECOVERING**: read-only investigation and narrowly scoped recovery actions; never presented as actively trading.

These are *proposed* user-visible operating modes and authorization gates, not merely settings on the current `lab paper` command.

## 2. First-time onboarding

The setup wizard (eventually `lab setup`, read-only until deliberate actions are approved) must guide:

1. **Workspace:** choose a local project folder, data location, backup/restore policy and immutable audit store. Show disk/memory estimate and key security boundaries.
2. **Scope:** confirm asset class US equities, paper-only, approved exchanges, capital/risk limits, markets and accepted data licenses. Present explicit trade/non-trade boundaries.
3. **Data sources:** connect a research provider and a verified paper market-data source; validate date alignment, corporate actions and point-in-time coverage. Research may proceed with marked lower-grade data; qualifying a strategy cannot.
4. **Research engine:** select local/provider AI, maximum CPU/GPU/LLM budget per day, experiment count, wall-time limits and prohibited operations. Test isolation with no broker access.
5. **Paper broker (optional):** connect IB Gateway on paper-only ports, read and confirm the exact DU account, require MFA completion; **start in read-only connectivity smoke mode**, not auto-trading.
6. **Alerts:** select channels, send a test message, configure escalation if the channel is unreachable.
7. **Readiness report:** show separate checks for research readiness, data readiness, paper connection, recovery completeness and deployment authorization; any unmet prerequisite is explicitly named.
8. **Finish:** land in **Overview** with `RESEARCH` or `SHADOW` mode; order sending remains disabled by default.

First launch is not a trading event. No secret is ever printed in generated diagnostics or exported reports.

## 3. Normal research-to-paper journey

```text
Create project
    -> Ingest & validate dataset
    -> Formulate hypothesis / delegate bounded AI research
    -> Review experiment artifacts and failures
    -> Confirm full statistical evaluation
    -> Authorize one sealed holdout access
    -> Review strategy artifact hash & risk preview
    -> Human approval (or rejection)
    -> Deploy to SHADOW (compare expected vs actual session behavior)
    -> Qualify with supervised paper test and broker reconciliation
    -> Independently review unattended-paper release gate
    -> Run scheduled sessions -> monitor -> alert/recover/stop
```

### A. Research project

**User input:** hypothesis or research objective, universe, time horizon, benchmark, cost assumptions, search budget and "stop when" criteria. Defaults are conservative; every important assumption appears in a preflight preview.

**System output:** `project_id`, exact immutable configuration hash, dataset version, **trial ledger** (including failed and abandoned ideas), progress, resource spend and report location. AI may propose/run tests in the isolated research plane but cannot approve its own strategy or request hidden holdout access.

**User experience:** compare candidates on matched baselines and confidence intervals; filter by cost sensitivity, walk-forward stability and number of trials. Show negative evidence as prominently as positive evidence. Return clear `INCONCLUSIVE` if assumptions fail.

### Market Intelligence workspace

A historical diagnostic page uses the exact `lab analyze` report schema.
Show as-of session and dataset quality before displaying numbers. Group analysis
into market breadth, per-asset momentum/risk, historical tail loss, liquidity
proxies and covariance/correlation matrices. Heatmaps are always accompanied
by numerical values and explanations for null/undefined relationships.

A separate future **Forecasts** view may display probability distributions
and model calibration only after a versioned forecast model and independent
validation exist. Never display descriptive labels as predictions, or allow
`EXPLORATORY_ONLY` analysis to supply an order permit.

### B. Strategy review and approval

The primary screen/document is an **evidence dossier**:
- statement of causal idea, assumptions and environments tested;
- data provenance, universe membership, exact train/test/holdout boundaries;
- number of variants tried, statistical caveats, return/distribution/drawdown/capacity;
- performance under pessimistic fills/fees, regime breakdown;
- open critic objections, security review and deterministic replay results;
- exact artifact SHA-256, approved parameters, bounds and broker-account scope;
- explicitly listed **reasons not to approve**.

User can **Reject**, **Request more research**, or **Approve for shadow**. Approval is scoped to a specific artifact, risk policy, data class, account and expiry; editing any of these invalidates approval. **No one-click direct research-to-unattended trading transition.**

### C. Shadow and supervised paper

The system shows each session as `COLLECTING → SEALED → RISK_REVIEWED → NO_TRADE | WOULD_TRADE → (supervised) ORDERS_SUBMITTED`.
For every expected stock/sleeve, users can inspect last finalized bar, age, provider, target, approval and why a trade was skipped. Shadow orders are predictions, never broker API calls.

Before supervised paper starts: validate broker account, open orders, full positions/cash/fills, risk state and notification channel; **request a deliberate operator approval**. The trade-plan preview discloses order type, TIF, size, estimated worst-case fees, buying-power reservation, venue/session validity and cancellation constraints.

### D. Unattended paper operation

Only a qualified, immutable release can be scheduled. Dashboard shows:
- **Mode:** UNATTENDED_PAPER (or HALTED/RECOVERING)
- **Broker:** connected/authenticated DU account and last *complete* reconciliation time;
- **Today:** exchange session and `DATA_WAITING` / `DATA_INVALID` / `SEALED` / `NO_TRADE` / `ORDERS_PENDING` / `SETTLED`;
- **Risk:** equity/buying power, daily caps, high-water mark, max drawdown and latest independent mark;
- **Orders:** acknowledged, outstanding, partial, cancel pending, unknown, filled;
- **Research:** bounded autonomous experiment jobs *separate* from broker operations;
- **Your attention:** action-required incidents and upcoming release reviews.

At the end of a session, generate a dated, reproducible report with input snapshot, proposed and actual orders, fills/costs, P&L, reconciliation, decisions skipped, uncertainty and any exceptions. No alerts when nothing noteworthy changed beyond the chosen digest cadence.

## 4. Incidents and recovery journey

All incidents have unique ID, severity, detected timestamp, impact, correlated event IDs, suspected causes, state, owner and timeline.

**Example: submit timeout after request sent.**
- System transitions to `HALTED / RECOVERING`, blocks *new* submissions and persists an `UNKNOWN` order intent.
- UI says: "**We cannot prove whether IBKR accepted this order. No retry has been sent.**"
- Shows broker order IDs, intended quantity and account, cached vs broker observations, data coverage, and whether further fills are possible.
- Allowed next action is **Run authoritative reconciliation**; an operator may request broker-history import. Until outcomes are proven, no `Resume` action appears.
- If broker history is incomplete, incident remains OPEN. Human "acknowledged" is not synonymous with "resolved".
- Recovery requires a logged review of balances, fills, open orders, risk state and data integrity; only then issue a new, scoped authorization.

**Kill / flatten difference:** `Halt` prevents new orders and begins verified cancellation, while `Request flatten` is a separate high-risk operation that may not be executable during a halt or outside market hours. Show `CANCEL_REQUESTED`, `CANCELED`, `PARTIALLY_FILLED`, `FLATTEN_PENDING`, `FLATTEN_FAILED`, or `FLAT_CONFIRMED` based on broker evidence. Never show "closed" after merely sending a cancel/market instruction.

## 5. Lifecycle and communication constraints

- **Autonomy:** agents can search for ideas and run bounded research without supervision; live order dispatch never gains permissions by virtue of a good result.
- **Notifications:** "FYI", "Needs review", "Trading blocked/critical" with correlation ID; mobile alert contains no account secret and links to authenticated detail view.
- **Acknowledgement:** marking an alert read does not override a halt.
- **Approvals:** explicit description of consequence and exact artifact/account/order scope, short validity, reason and anti-replay. Dangerous actions use a separate confirmation and audit.
- **Account authority:** roles `Viewer`, `Researcher`, `Reviewer`, `Paper Operator`, `Administrator` even for single-user setups; separate execution rights from AI service rights.
- **Accessibility:** status is always expressed in text/icon as well as color, UTC + user timezone + exchange session date shown, log timestamps unambiguous, keyboard/CLI parity.
- **Operational honesty:** show `UNKNOWN` rather than `GREEN` for stale/unavailable data; warnings are persistent and actionable, not transient toast messages.
- **Exports:** human report + JSON manifest with hashes and timestamps. Secrets redacted by default.

## 6. Example daily experience

Morning: user sees "**No action required** — the paper account was reconciled; last market session is complete; no open incidents." Meanwhile the AI can research ideas under its own budget.

After the New York close: a session card moves from collecting to sealed; a shadow/paper decision is published. If an expected ETF bar never arrives, the session is **SKIPPED: missing SPY finalized close**, with no guessing. If broker rejects an order, user sees the exact order/reason and whether subsequent rebalancing is blocked.

Evening: user receives a compact report and can compare predictions to fills. A new strategy appears under **Needs review**, with evidence. Nothing automatically promotes it to a broker-connected deployment.

## 7. Wireframe-level views

### Overview
```text
TRADING LAB | Workspace: US Equity Research       Mode: SHADOW
Status: DEGRADED (market data incomplete)       Time: UTC / New York
--------------------------------------------------------------------
ATTENTION (1)  Session 2026-10-22: SPY finalized bar missing
Broker         DU account verified | reconciled: YYYY-MM-DD HH:MMZ
Risk           No permits issued | pending orders: 0 | halt: no
Research       4 experiments completed / 2 running | budget 34%
Today          COLLECTING -> INVALID; no order submission possible
Actions        [View incident] [Check data] [Run diagnostic]
--------------------------------------------------------------------
Recent runs    run-id | hypothesis | validation | code digest | verdict
```

### Strategy dossier
```text
Strategy: trend_alpha@sha256:...  | Status: VALIDATION_FAILED
Economic thesis / failure evidence / dataset manifest
Train | walk-forward | null | final holdout (sealed unless authorized)
Portfolio risk | capacity | fees / slippage | stress regimes
Reviewer objections and reproducibility certificate
Actions: [Inspect source] [Re-run on training] [Reject] 
         [Request more research] [Approve for SHADOW (only if qualified)]
```

### Incident
```text
CRITICAL #INC-000241: UNKNOWN ORDER STATE
Paper account: masked DU***   | Session: 2026-10-22
Order: intent-... BUY 10 (client-ID..., broker ACK missing)
Status: New order submission LOCKED
Verified: intent committed | Missing: authoritative execution-history response
Steps: [Fetch broker snapshot] [Compare fills/fees] [Review reconciliation]
Disabled: Resume / Resend / Flatten until prerequisites are met
Audit: who, what, when, version, proof, reason
```

These are **design mockups**, not screenshots of implemented pages.

## 8. Acceptance tests for UX

| ID | Scenario | Expected |
| --- | --- | --- |
| UX-01 | Fresh user installs app without paper broker | Research works, broker actions disabled, honest status |
| UX-02 | Data version lacks delisted names | Clear badge/wording "not suitable for qualification" |
| UX-03 | AI proposes strong backtest with open critic issue | No approve-for-paper action |
| UX-04 | Unauthorized role tries promotion | Denied and audited |
| UX-05 | Broker reports two accounts / wrong DU | Critical block, no orders |
| UX-06 | Broker ACK missing | UNKNOWN incident, never "retry" button |
| UX-07 | User requests halt while no new data arrives | Submission stops independently of signals |
| UX-08 | User taps "Resume" with unresolved order | Disabled with exact unmet prerequisites |
| UX-09 | An alert is acknowledged | Incident is still unresolved; trading remains blocked |
| UX-10 | Market half-day / no finalized stock close | Correct session and actionable skip reason |
| UX-11 | A strategy file changes after approval | Approval revoked; no silent hot reload |
| UX-12 | Mobile notification viewed offline | No control action executed; authenticated review required |
| UX-13 | Daily report export | Reproducible hashes, no leaked secrets |
| UX-14 | Display timezone changed | Calendar/day identity unchanged; instants converted accurately |
| UX-15 | Browser/UI unavailable | CLI can inspect state and halt safely; no unauthorized bypass |

**All UX acceptance tests remain to be implemented.**
