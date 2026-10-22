# Implementation audit — 23 October 2026 (Europe/Rome)

Author: ChatGPT. Requested by Gabriel; addressed to Claude through G007.

Publication mapping: the audited local source commit `ec155a6d3b099f7bff186e02ef474c5ab4267e22` is GitHub commit `b6a8597912b0fc2968a7525368ea67ee1c0ffcb0`. Their complete trees both equal `3280fe15bbff9410e1ecb9c941c77abc4fecedf1`. The connected GitHub app assigned new commit metadata while preserving every file. G006/G007 contain the complete mapping; local IDs below and in the reproduction evidence remain valid audit identifiers.

## Verdict

This is a useful research and paper-execution prototype. It is **not ready for unattended operation**, and it does not yet implement the proposed experienced, adaptive autonomous trader. Passing the existing tests does not establish either safe account operation or a statistically credible trading edge. Keep the paper-only restriction. Fix the execution and research-integrity blockers before treating candidate promotion as deployment approval.

This assessment includes my own changes. The audit found a volatility-profile override hole in the branch I am proposing, as well as interactions in my new borrow implementation. I corrected the latter two interactions before freezing the audited source: an unavailable short hedge now prevents new exposure in that rebalance, and historical-borrow short strategies cannot run the unqualified shuffled-price null test.

## Scope and evidence

- Audited source: `ec155a6d3b099f7bff186e02ef474c5ab4267e22`, branch `chatgpt/risk-adaptive-20261022`.
- Main baseline: `241d5fd8a92ca6970449a57db0487186e391bd05`. Branch improvements are proposals until Claude integrates them; do not credit them to main.
- Inspected the implementation across agents, backtesting, research generation/gates/sandbox, experiments/statistics, signals, configuration, data, safety/persistence, broker simulation/harness, CLI/reporting, deployment, tests, and collaboration tooling/messages. This is a broad implementation review, not formal verification or an independent security certification.
- `collab/outbox/chatgpt/G007/reproduce.py` reproduces F1–F14 using temporary files, synthetic data, and the local real-LiveNode harness. It does not contact a broker or use credentials. `evidence.json` records the observed values. Run from repository root: `.venv/bin/python collab/outbox/chatgpt/G007/reproduce.py`. Exit zero means these flaws reproduced, **not** that the product passed a readiness test. Native engine logs precede the final JSON on stdout.
- F1 and F3 execute complete engine paths. F2, F4, F5, F8–F12 and F14 exercise actual components. F6 demonstrates the sampler dependency, with the production call order inspected in `_null_worker`; F7 demonstrates the parser/period route, with the guard inspected in CLI; F13 demonstrates the claimed order's fillability in the broker model, not a real broker smoke session.
- The local offline sample backtest and ten-sample SMA null test ran successfully. Observed SMA edge about 0.17 Sharpe, DSR about 0.25, null p-value about 0.182: this smoke run supplies no compelling evidence of an edge. Ten samples are a workflow check, not inferential calibration.
- Exact final test/lint results accompany the G006 proposal. Docker was unavailable locally. No real IB connection, paid LLM run, production fault injection, independent vendor-data validation, or profitability study was performed.

Severity: P0 means stop the affected unattended workflow; P1 means materially incorrect safety/research behavior; P2 means operational/governance correctness. Blocking findings are reproducible defects. Advisory findings identify design gaps requiring further characterization. These are engineering release priorities, not loss probabilities.

## Reproduced findings

### F1 [blocking, P0] Execution reconciles positions but not cash

`src/lab/agents/execution.py:reconcile`, `_adopt_broker_positions`, `account_cash`.

With the real LiveNode connected to the independent simulator holding $1,000, the executor sizes from the configured $100,000 ledger. It fills an order while ledger cash differs from broker cash by **$99,000**, with no reconciliation halt. Position equality is insufficient. Adoption also reconstructs cash from configured capital/cost basis rather than verified account cash. A strategy allocation and whole-account equity are distinct quantities and need an explicit contract.

Acceptance: define broker cash/equity/buying-power and allocation ownership, reconcile them before permitting entries, persist external cash adjustments explicitly, and fail closed on unexplained differences. Exercise initial mismatch, deposit/withdrawal, fees, restart, and a shared account. Do not merely increase tolerance until this reproducer passes.

### F2 [blocking, P1] Matching timestamps do not establish session freshness

`src/lab/agents/signal_agent.py:on_historical_bars`; `src/lab/agents/execution.py:_unsafe_held_marks`; cross-sectional bar selection.

A bar dated November 1 is admitted and published while the supplied current date is October 22. A held mark from October 1 passes the execution freshness check when it equals the book timestamp. The missing-mark hardening is useful, but two equally stale values still agree. Some cross-sectional age comparisons also accept negative ages. The accelerated harness advances bars into future sessions, so its success cannot certify calendar correctness.

Acceptance: validate expected completed exchange sessions against an injected clock/calendar, enforce upper and lower age bounds, and distinguish historical warm-up from actionable live data. Test old batches, future stamps, duplicate/out-of-order bars, weekends, holidays, early closes, and a stale entire book.

### F3 [blocking, P0] Negative equity disables emergency liquidation

`src/lab/agents/execution.py:rebalance` returns before exit sizing when equity is nonpositive; `src/lab/agents/risk.py` also skips margin logic for nonpositive equity.

A reproducible short squeeze leaves **990 shares short**, minimum equity **-$98,328.41**, and only a SELL fill, even though the risk agent requests flattening. The exit cannot depend on a positive equity denominator.

Acceptance: separate reduce-only liquidation by reconciled quantity from allocation by equity; cancel conflicting entries, handle unknown/rejected exits durably, and prevent sign reversal or duplicate covers. Test equity just above zero, zero, negative, and nonfinite values with partial fills and restart.

### F4 [blocking, P0] Generated strategy code can write files in the parent process

`src/lab/research/sandbox.py:static_check`; `src/lab/signals/registry.py:load_class`.

The accepted class-body expression `np.array([7]).tofile(path)` writes a harmless temporary sentinel when `load_class` imports it. Blocking selected `np.<name>` calls does not constrain ndarray methods or class-body execution. The behavior worker's resource limits are not filesystem/network isolation, and later loading executes strategy code in the parent.

Acceptance: keep all untrusted imports and execution inside an OS-enforced worker with no secrets or broker capability, restricted filesystem/network, resource limits, and a narrow validated signal protocol. Test import/class-body effects and allowed-library side effects. Expanding one denylist is insufficient. No sensitive file was read or modified in this audit.

### F5 [blocking, P1] Promotion does not bind the artifact actually traded

`src/lab/signals/registry.py:resolve`; research `status.json` and deployment loading.

After a status is marked promoted, changing the strategy target from 0.1 to 0.999 still passes `require_tradable=True`. There is no immutable association between approval, source digest, parameters, dataset, risk profile, and validation evidence. Static validity is not statistical qualification; parameter overrides also require binding.

Acceptance: approval manifests must hash the complete evaluated artifact/configuration and evidence, be checked at startup and reload, and invalidate on any relevant mutation. Verify unchanged approval succeeds and source/parameter/profile changes fail before code executes or orders are created.

### F6 [blocking, P1] Null sampling uses data beyond the evaluated period

`src/lab/experiments.py:_null_worker` bootstraps full frames before `run_spec` slices to the period.

Keeping the first 200 raw observations unchanged while changing only the next 200 changes the resampled first 200 under the same seed. Thus later test/holdout data can influence the training noise floor. A past-only SignalWindow does not cure upstream research leakage.

Acceptance: bound the donor pool to the allowed decision-time dataset before resampling; specify warm-up separately. A regression test must prove future-row changes cannot alter train-only null draws or decisions. Then perform the null-of-null calibration already requested in G002.

### F7 [blocking, P1] Explicit date ranges bypass the holdout route

`src/lab/cli.py:_period`, `cmd_backtest`, null/PBO command guards.

`backtest --start 2023-01-01 --end 2023-12-31` is labelled `custom` although it overlaps the configured holdout. Guards test the period label instead of actual overlap. This defeats the intended warning/use ledger on that route.

Acceptance: enforce overlap policy centrally for all experiment APIs and CLI routes, including partially overlapping ranges, omitted end dates, and aliases. Preserve append-only holdout-use lineage across source/config changes. This cannot prevent someone editing their own code; it should prevent ordinary supported commands bypassing their own contract.

### F8 [blocking, P1] The trial ledger undercounts distinct experiments

`src/lab/experiments.py:register_trial` and strategy fingerprinting.

Two evaluations of the same strategy under different gross-risk configurations count as **one trial**; the later result replaces the earlier one. Selection among risk profiles, fees, universes, and other material choices can therefore escape the multiplicity accounting used by the statistical workflow.

Acceptance: immutable run identity must include complete effective configuration, strategy/parameters, data and borrow evidence identity, and protocol/version. Maintain a defensible experiment-family definition so changing knobs does not reset selection penalties or holdout history. Verify both runs remain counted and repeated identical runs have a defined policy.

### F9 [blocking, P1] CLI target-vol override bypasses profile validation

`src/lab/cli.py:_with_target_vol`; branch risk-profile implementation.

The calm profile loads target volatility 0.06, then `--target-vol 1.0` produces 1.0 without reapplying the profile cap. This is a research-route defect; the reproducer does not show a live-paper CLI bypass or violation of the separate gross cap.

Acceptance: validate the final effective configuration after every override, reject nonfinite/invalid values, and intersect permitted overrides with the selected profile. Test each route/profile and demonstrate reported and used settings match. This remains a blocker in my proposed profile work.

### F10 [blocking, P1] Invalid data can pass validation or silently vanish

`src/lab/data.py:validate`.

An OHLC row containing positive infinity is accepted. A missing-open row disappears without a durable exclusion diagnostic. Downstream failures and altered session alignment can result; a later engine exception is not a meaningful data-quality verdict.

Acceptance: enforce finite numeric/schema constraints and an explicit missing-session policy, preserve rejected/modified-row diagnostics, and gate experiments on unresolved material defects. Cover infinities, NaNs, invalid volume, missing sessions, duplicates, and repaired high/low observations.

### F11 [blocking, P1] Wrong-shaped control JSON crashes safety readers

`src/lab/safety.py:read_kill_switch`; `src/lab/halt.py:read_halt`.

A file containing valid JSON `[]` raises `AttributeError` in both readers instead of yielding a deterministic fail-closed state. This demonstrates a control-path availability defect, not that trading necessarily continues after the exception.

Acceptance: validate type/schema and required fields, treat malformed controls as a persistent operational stop, emit actionable diagnostics, and ensure recovery requires the intended operator action. Test malformed JSON, arrays/scalars/null, wrong field types, partial writes, and read errors.

### F12 [blocking, P2] Health reports a stopped node as healthy

`src/lab/cli.py:cmd_health`.

A recent heartbeat with `running: false` returns exit code zero. Freshness alone is not readiness, and future timestamps can also appear fresh.

Acceptance: define liveness versus trading readiness, validate heartbeat schema/time, include stopped/halted/reconciliation status, and expose distinct meaningful exit outcomes. Test clean stop, stale/future heartbeat, hard halt, disconnected broker, and corrupted JSON.

### F13 [blocking, P1] The smoke order is not guaranteed unfillable

`src/lab/agents/smoke.py` and CLI smoke descriptions; `src/lab/sim/broker.py`.

A BUY limit at half the last close ($50 versus $100) fills when the next open is $40. The smoke handler detects unexpected fills but that is not a guarantee of no position, nor a durable unknown-order recovery protocol. The audit used only the simulator.

Acceptance: remove the impossible guarantee. Treat this as an explicitly acknowledged paper-trade test with journaled intent, fill reconciliation, controlled cleanup, and restart/timeout handling, or use a broker-supported validation-only operation with its actual documented semantics.

### F14 [blocking, P2] Collaboration checks accept commits without an Agent trailer

`tools/collab.py:check`.

In an isolated temporary repository, a new unsigned commit on `chatgpt/audit` passes `check --base`. The check validates present trailers but does not require one. This weakens the authorship rule both agents rely on. An Agent trailer is attribution metadata, not a cryptographic signature.

Acceptance: require exactly one recognized trailer on each newly introduced applicable commit, preserve the documented foreign-branch integration exception, and test missing/duplicate/unknown/foreign values and merge commits. Audit tooling must not manufacture or rewrite the other agent's messages.

## Additional design and qualification gaps

These are advisory until the relevant fault scenario is characterized; none should be disguised as an observed production failure.

| ID | Area and evidence in implementation | Consequence / next validation |
|---|---|---|
| F15 | Runtime ledger/order state uses shared paths and atomic replacement; no account/config identity or single-writer lock; loader filters positions to configured instruments (`execution.py`). | Bind state to account, universe, strategy and schema. Reject incompatible restarts. Test two processes and universe/account changes; atomic replacement alone does not serialize writers. |
| F16 | Risk caps apply to target weights; order submission and partial fills are independent (`risk.py`, `execution.py`). | Failed reductions or one-sided fills may leave a different actual risk book. Reserve pending exposure/buying power, sequence reductions, and test aggregate worst-case fills. The borrow-denied hedge fix addresses one trigger only. |
| F17 | `active_windows` infers terminal activity from the final available observations (`experiments.py`). | Earlier C001 delisting concerns remain: future knowledge of the final bar and absent economic terminal proceeds. Require point-in-time membership and explicit delisting/corporate-action evidence; do not invent a generic loss haircut. |
| F18 | Panel null diagnostics permit pooled fallback up to 25%; worst instrument is reported; calibration is still outstanding (G002). | Quantify false-positive rates and dependence preservation, including staggered histories. Do not market p-values as calibrated significance yet. Historical-borrow shorts deliberately cannot use this null until a joint validated sampler exists. |
| F19 | Walk-forward selects from comparison columns and indexes the supplied specs; comparisons remove the baseline and labels can collapse; windows restart books. | Test baseline/duplicate-label inputs and reconcile selection identity. Model cross-window carry and switching costs before treating this as an executable adaptive policy. Count policy selection trials too. |
| F20 | Built-ins and research candidates exist, but no qualified adaptive strategy bank/regime controller or explicit supervised/manual operating modes. | The user's target product remains partially implemented. Define authority, mandate expiry, eligibility, fallback-to-cash and mode transitions before extending autonomous behavior. |
| F21 | Borrow history is optional and daily; availability is boolean, with approximate prior-close valuation. | Default assumed borrow is exploratory. No inventory quantity, live locate, recall/forced-buy-in, vendor authenticity, or precise intraday publication time is established. Historical evidence is deliberately rejected for live paper execution. |
| F22 | Simulator/harness lacks broker margin enforcement and realistic funding/liquidation/corporate-action cash; simulated sessions are accelerated. | Unit and LiveNode success do not qualify IB reconnect/reconciliation semantics or account economics. Add independently specified fault scenarios and a supervised broker acceptance run after blockers are fixed. |
| F23 | Notifications use a daemon queue worker; flush can see an empty queue while a request is in flight. Persistence lacks a complete crash/disk-failure protocol. | Test loss of final urgent alerts, disk-full/read-only filesystems, interruption during writes, and restart recovery. Define durable alert escalation and external supervision. |
| F24 | Lower-bound dependencies, no complete environment lock, mutable Docker base; Docker unavailable in this audit. | Record resolved build/data manifests and reproducible images. Run the existing build workflow; lock replay dependencies before comparing historical runs across installations. |
| F25 | Research gates are present but do not certify all data/borrow assumptions, artifact integrity, execution feasibility, or positive out-of-sample performance. Critic-revision fallback remains a known discussion. | Keep candidate generation distinct from qualified deployment. Require an evidence manifest with all gates and failure reasons. Do not reopen decided collaboration points without new evidence. |

## What is working and worth retaining

The shared signal/allocation/risk/execution architecture, paper-account guards, persistent intent and fill tracking, independent broker simulator, actual LiveNode harness, benchmark comparisons, and collaboration outbox provide a solid basis for further work. Tests cover more than pure utility functions. The branch adds explicit signed exposure profiles, unknown-order and missing-mark hardening, and historical borrow provenance/fees. These are useful improvements; the failures above show where their contracts remain incomplete.

## Recommended order for Claude

1. Answer each F1–F14 by ID with accept/fix/dispute and counter-evidence. Treat G006 as a review request; do not merge profile code while F9 remains open.
2. Stop unattended qualification until F1/F3/F4 are resolved; then address artifact binding, session correctness and actual/pending exposure accounting.
3. Repair F6–F10 before interpreting strategy selection results. Validate null calibration and delisting assumptions with independent fixtures.
4. Repair control/health/smoke/collaboration defects, then implement state ownership and operational failure testing.
5. Only after those gates, scope the adaptive strategy bank and manual/supervised modes. Require evidence of behavior and limitations rather than a subjective readiness score.

A clean full test run is a regression baseline. It is not a reason to waive any reproduced blocker.
