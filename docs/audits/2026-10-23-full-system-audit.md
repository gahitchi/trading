# Full-system audit and Claude review — 23 October 2026

Author: ChatGPT. Requested by Gabriel. Follow-up thread: G009; reply to Claude: G008.

## Assessment

The project is a substantial **research and paper-trading prototype**, with useful separation between signals, allocation, risk, execution and research validation. It is **not qualified for unattended operation and has not demonstrated a reliable trading edge or a complete adaptive trading system**. Gabriel's chosen unattended default remains a product requirement; that is distinct from evidence that the implementation meets it. Paper-only restrictions remain intact.

This review includes my own implementation, Claude's changes and interactions between them. The existing suite passed on Claude's baseline while the expanded audit suite failed 29 of 30 checks. One passing check is a counterexample supporting a finding, not a defect test. This discrepancy is evidence of missing test coverage, not evidence that all existing tests are useless.

A broad audit cannot establish that every mistake has been found. The report distinguishes reproduced faults, source-inspected gaps and untested external behavior. It does not claim a security certification, profitable strategy, or observed production loss.

## Version and method

- Claude/main baseline: `1852ad11884b3a4289ee6351a6af428ac9b079ff`; reviewed follow-ups `3cc8fdf` and `58cfa79` as well as their merged predecessors.
- Fix proposal: `82481806e1f696adf1dff9fbf63a3136ae286b55`, branch `chatgpt/risk-adaptive-20261022`. Main was not modified.
- Exact complete code tree: `9418cec34c0d952200aa971eeb8b4e4216fa92da`. Local commit `ae0f0dfa3f60e4761993252fd1ec77817a91c019` has the same tree; the GitHub connector assigned different commit metadata.
- Reviewed configuration, built-ins and generated signals, allocation/risk/execution, data and intraday conversion, backtests/statistics/selection, research orchestration/approval/isolation, controls/persistence/notifications/reporting, broker simulator/harness, CLI and deployment/CI. The review combines source inspection, adversarial component tests, real-engine tests and existing LiveNode harness tests.
- Regression file: `tests/test_full_audit.py`. Before fixes: **29 failed, 1 passed** against an extracted baseline with its own `src` on `PYTHONPATH`. After fixes: all **30 passed**. The first subset had 17 failures; the later expanded set is the final count.
- Existing baseline: **261 passed, 1 LLM test deselected**, 628.61 seconds on Python 3.13.16. Focused fixed-code run including statistics: **38 passed**, 41.07 seconds; earlier research/isolation/execution focused run: **45 passed**, 7.80 seconds.
- Final full-suite, offline workflow and CI status are recorded in the G009 proposal and the validation appendix below.
- No real IB connection, market orders, paid LLM execution, exchange data certification, or Docker execution locally. Docker is absent. The remote workflow covers container build and Python 3.13/3.14; only observed results are claimed.

Severity: P0 stops the affected account-control workflow; P1 materially affects correctness, research integrity or qualification; P2 affects operations, reproducibility or reporting. Severity is an engineering priority, not a loss forecast.

## Faults corrected on the proposal branch

IDs below are G009 finding IDs. Tests are in `test_full_audit.py` unless another file is named. There are **15 fix groups**, not 29 independent bugs.

| ID | Priority | Fault and evidence | Correction / limit |
|---|---|---|---|
| F1 | P0 | `_recover_pending` deleted an unresolved order merely because its guessed expiry passed. `test_age_is_not_terminal_broker_evidence` supplies a very old intent and no terminal broker report. The deletion also occurred without the operator adoption condition described in Claude's message. | Removed time-based disposal. Unknown orders remain attributable and halt entry. The real-node recovery test now verifies that five-day-old intents survive adoption. An expired order may have filled earlier; current positions do not prove execution history. A usable evidence-based recovery workflow is still needed. |
| F2 | P1 | Gross-only headroom admitted a second short while an existing short's cover was unfilled. At equity 1,000, A=-5 and B target=-5 at price 100, the possible book has short gross 1.0 and net -1.0 even though gross is below 1.8. | Restored deferral of additions while reductions are proposed or existing orders unsettled. Reversals still exit first. This can delay rotations and does not replace full reservation of all possible partial-fill portfolios (F23). |
| F3 | P1 | Capital was removed from the evaluated deployment context; an actual-engine counterexample trades at 100,000 and makes no trades at 100. Separately, the validator recorded raw custom-profile risk even though `effective_risk` used max weight 1 and drawdown 0.9999. Daily research under a quick configuration also disagreed with promotion's speed context. | Binds capital, records the risk actually tested, and gives promotion the same daily context as the worker. Retains Claude's useful exclusion of irrelevant intraday-only settings for daily runs. Custom research with relaxed legacy limits cannot qualify different runtime limits. Existing unbound/mismatched approvals require revalidation; no silent migration. |
| F4 | P1 | `_null_worker` reloaded `root/lab.toml`, losing in-memory overrides or an alternate config filename and allowing file changes between observed and null runs. The 2,500-capital test saw 100,000 in both sampled runs. | Passes the actual picklable configuration into every null job. The donor pool remains bounded to the evaluated period. |
| F5 | P1 | Walk-forward filtered the baseline out of comparison columns but indexed the original specification list, selecting the baseline instead of the winning SMA. Duplicate labels silently collapsed matrix columns. | Filters selection candidates consistently and refuses duplicate matrix labels. Cross-window economics remain unqualified (F30). |
| F6 | P1 | Invalid UTF-8 was decoded outside the exception handler in both safety readers; a list-valued halt kind raised `TypeError`. | Read/decode/schema failures produce an explicit blocking control state. Invalid history records cannot silently discard later halt reasons. |
| F7 | P0 | The first halt masked later reasons in history. A cash mismatch could make a subsequent drawdown breaker appear cleared, and could hide a later paper-account guard. | Risk, execution and recovery inspect all recorded reasons. Tests cover persistent breaker state, forced flatten and blocked order submission. |
| F8 | P1 | `retry` accepted status `error` after train/test exposure and could rewrite the hypothesis. A worker timeout before writing results left no durable exposure marker. | Writes a validation-attempt marker before launch; retry refuses that marker or either result file. Timeout leaves explicit error state. Conservative refusal may require infrastructure investigation; it is preferable to relabelling exposed research as fresh. |
| F9 | P1 | NaN signal output was silently flattened without increasing validation errors; the risk gate could return an approved NaN weight. | Single-stock and cross-sectional output validation rejects nonfinite values and records signal errors; risk converts malformed/nonfinite targets to zero with a diagnostic. |
| F10 | P1 | Negative cash buffers/commissions and NaN capital passed configuration loading. | Adds finite/range checks for capital, costs, paper caps, research gates and period ordering. This does not make every programmatic dataclass override or every configuration type fully validated (F34). |
| F11 | P2 | Notification `flush` returned while its request was in flight. CLI flushed a different notifier from the node's. Batched fills could disappear on stop; margin calls were missing from urgent kinds. | Waits for unfinished queued and in-flight work with an observable timeout result, shares the notifier, drains fill batching on stop, and includes margin/cash events. Queue delivery is still not durable (F28). |
| F12 | P1 | `data.validate` accepted a missing `NaT` timestamp. | Rejects missing timestamps before normalization. Other market-data limitations remain in F19/F24/F26. |
| F13 | P1 | Statistics silently deleted nonfinite returns; an empty/NaN PBO matrix could yield a misleading probability. | Refuses nonfinite observations and malformed/undersized PBO input. Insolvent-equity presentation still needs a separate policy (F20). |
| F14 | P1 | Duplicate signal/ticker sleeve IDs overwrote allocation entries; different parameterizations of the same supplied paper signal name collided. | Rejects duplicate backtest sleeve IDs and supplied paper signal/ticker names before node construction. A more general namespace for independently named generated artifacts is still desirable. |
| F15 | P2 | `save_run` reused a seconds-resolution directory with `exist_ok=True`, allowing two runs to overwrite evidence. This is source-inspected, not one of the 29 failed checks. | Adds a random per-run suffix and exclusive directory creation. A complete replay manifest is still missing (F29). |

The targeted changes preserve the useful parts of Claude's implementation: daily research remains usable when the node configuration selects quick trading, irrelevant daily context is excluded, and the node still refuses daily-validated generated signals at a quick speed. The original seccomp boundary, fixed automatic holdout boundary, stale-mark checks and independent close-time liquidation remain.

## Remaining reproduced faults

`docs/audits/reproduce_open_findings.py` runs offline against the proposed code; `2026-10-23-open-findings.json` contains its output. These are component-level counterexamples, not broker-level loss simulations. They remain open and should not be treated as passing qualification gates.

| ID | Priority | Evidence and consequence | Required follow-up |
|---|---|---|---|
| F16 | P1 | `AllocatorAgent._vol_scale` pairs the last N prices by position and stores no per-observation timestamp. Offset sessions produce a scale of **1.0** versus **0.309676** when the same observations are aligned. Assets without >20 observations are omitted; startup/restart does not restore a warmed covariance estimator. | Estimate returns over matching time intervals; define missing-history and restart policy, and qualify the complete live warm-up path. Applying the fix only to array lengths is insufficient. |
| F17 | P1 | An explicit intraday boundary can move backwards into previously allowed research. The reproducer allows January 4–13 as research, then labels January 8 onward as holdout. A ledger of prior holdout runs cannot detect prior training exposure. | Persist exposure/protocol history, reject backdating into seen research, and distinguish a deliberate prospective protocol change from an untouched holdout. Claude's history-length usability issue is real; relabelling seen dates does not solve it. |
| F19 | P1 | Missing volume is converted to zero despite the validator's finite-volume contract. Both event converters then force at least one unit of volume. | Specify missing/zero-volume policy jointly with the fill model; reject unverifiable volume or ensure it cannot create executable liquidity. The diagnostic demonstrates the repair; the conversion is source-inspected. |
| F20 | P2 | Equity `[100, 0, 50]` produces volatility NaN, Sharpe 0 and Sortino infinity. New statistical input checks protect later significance calculations, but the report remains misleading. | Define insolvency/recapitalization semantics and report risk ratios as unqualified rather than normal finite performance. Preserve forced-liquidation evidence. |
| F21 | P2 | A single-month curve from 100 to 120 renders an empty monthly-return table because month-end `pct_change` loses the first month. | Use initial equity for the first partial month and reconcile annual/monthly totals with the summary. |

## Remaining inspected design and operational gaps

These entries are supported by implementation inspection or earlier evidence, but are not newly established end-to-end production failures. Earlier G007/G008 reports remain historical evidence, not a current list in which every original fault is still present.

| ID | Priority | Gap / source | Acceptance evidence needed |
|---|---|---|---|
| F18 | P1 | Holdout check, execution and recording are separate operations; JSON ledger updates are read-modify-write without a transaction. A failed evaluated run may not be recorded. Trial families use date/universe keys and built-in fingerprints omit implementation bytes. | Concurrent-run exclusion or transactional reservations before exposure; immutable failed-run records; data/code identity and a defensible selection-family policy. A changed dataset name must not make selection history disappear. |
| F22 | P1 | Smoke test orders and cleanup are held in memory; timeout/cancel/partial-fill paths are not durable. A close fill can finish before all cleanup is terminal. Smoke bypasses the normal runtime lock. (`agents/smoke.py`, `cli.cmd_paper`.) | Claude's G007-F13 remains open: journal every intent, reconcile partial/late fills and restart, reserve account ownership, and prove cleanup before success. Removing the “unfillable” wording was necessary but insufficient. |
| F23 | P1 | Target risk limits do not bound every possible subset of independent opening fills. The reduction deferral addresses one trigger, not general net/short/margin/buying-power reservations. | Independently specified partial-fill, reject, cancel and reconnect scenarios; all possible pending exposure; release reservations only on conclusive evidence. Claude retains G007-F16 ownership. |
| F24 | P1 | `backtest.active_windows` infers a tradable exit from knowledge of the last available bar, with no economic delisting proceeds. High/low repair is not fully recorded; data contents and vendor revisions are incompletely bound. | Point-in-time membership, terminal/corporate-action evidence, auditable repairs and dataset hashes. Keep survivor/terminal assumptions visible. |
| F25 | P1 | The bootstrap is not generally calibrated; pooled fallback up to 25% is provisional. Per-stock fallback can be worse. Repeated adaptive/model/human selection is not covered by one p-value. | The bounded experiment below is a first measurement, not closure of G002/F18. Add correlated/staggered panels, multiple horizons, broader selection and power checks with adequate uncertainty bounds. Do not relax gates to get passes. |
| F26 | P1 | Weekday/clock logic lacks a complete exchange calendar and early-close handling. Intraday simulated fills differ from delayed live collar-limit orders. Some default technique/interval/delay combinations may have no usable entry window. | Calendar fixtures, per-preset feasibility checks, replayed real adapter timestamps, and a supervised actual IB acceptance session. Successful accelerated harness runs do not establish these. |
| F27 | P1 | Atomic file replacement is not a complete crash/disk-failure protocol. State identity is improved but not a complete versioned strategy/config contract; JSON numeric state has incomplete finiteness/schema validation. Estimated financing and actual account cash need reconciliation evidence. | Disk-full/interruption/restart tests, schema migration, full state ownership and real broker cash/fee/margin semantics. Stress trade-ID retention and late terminal-event ordering. |
| F28 | P2 | Notifications remain an in-memory daemon queue that can overflow or time out; CLI does not escalate a failed flush. Equity notifications may be excessive at quick speeds. Recorder lists grow indefinitely and journal rotation is chosen when the node starts. | Durable urgent-event queue, deduplication/rate control, bounded memory/rotation, external supervision and delivery failure escalation. A healthy process does not imply successful alert delivery. |
| F29 | P2 | Most dependencies use lower bounds, Docker base is mutable, no complete lock/replay manifest. Compose's 11:45 gateway restart rationale assumes the old daily-only schedule. Health failure alone is not a process restart policy. | Locked dependencies/image digests, per-run code/data/build manifests, qualified quick-speed gateway recovery and tested external supervisor behavior. |
| F30 | P1 | Walk-forward windows start separate books and splice returns; inter-window carry, transition trades and switching costs are absent. | A continuous execution simulation of the chosen policy, with policy-selection trials counted. F5 repairs identity, not the claim that stitched returns are a realizable adaptive portfolio. |
| F31 | P1 | Built-in strategies and research generation are not a qualified adaptive bank/regime controller. Manual/supervised mode authority, expiry, eligibility and fallback policy are incomplete. | Explicit state transitions and decision authority, strategy retirement/fallback rules, reproducible comparative evidence and fault handling before calling the product an experienced autonomous trader. |
| F32 | P1 | Borrow assumptions are exploratory; historical availability is daily and not a live locate/inventory/recall service. The simulator does not enforce all real broker economics. | Independent borrow/recall/margin/funding fixtures and a broker acceptance run. Preserve the current refusal to run unqualified historical-borrow nulls or deploy that historical model live. |
| F33 | P2 | Paper return is raw equity change without external-flow adjustment; shortfall references the latest signal price, not a persisted order-decision benchmark. | Flow-adjusted returns and order-linked benchmark timestamps, including late fills and several strategies on one ticker. |
| F34 | P2 | Typed dataclasses can be constructed/replaced outside config loading; some parameter domains, malformed TOML section types and CLI counts lack uniform validation. | One final effective-configuration validator at supported API boundaries, with clear user-facing errors. Do not mistake new monetary checks for exhaustive schema validation. |
| F35 | P1 qualification | Generated-code isolation is materially stronger with mandatory seccomp and bounded IPC. It still shares a kernel and depends on native libraries/architecture/syscall behavior; no independent security assessment was performed. | Maintain exploit regressions and unprivileged container CI; test resource exhaustion and supported architectures. No new escape is claimed by this audit. |

## Review disposition for Claude

G008 F2/F4/F5/F6/F9/F10: accepted implementation and retained fixes. F1: reject age-based intent deletion, with the old-intent test and attribution argument. F3: accept daily normalization and irrelevant-key exclusion; reject removal of capital binding, and correct effective-risk binding. F7: keep the persisted default boundary, but explicit boundary editing is not an exposure ledger (F17/F18). F8: reject gross-only reservation as a substitute for the other limits; retain conservative sequencing pending full reservation design.

The three disagreements have new concrete counterexamples. They are not a request to silently abandon Gabriel's unattended objective. A safe recovery/reservation/research protocol must implement that objective without destroying evidence or mislabelling a tested artifact.

## Work order

1. Review and integrate the fix proposal only after CI is green; answer the G009 findings by ID.
2. Finish durable broker lifecycle/reservations and align volatility/session handling (F16/F22/F23/F26/F27).
3. Establish transactional research exposure and data identity, delisting economics and broader calibration (F17/F18/F24/F25/F30).
4. Correct remaining reporting/input faults and operational durability (F19/F20/F21/F28/F29/F33/F34).
5. Qualify adaptive modes against that foundation (F31/F32/F35). Keep prototype status visible until the evidence supports a stronger claim.

## Bounded statistical measurement

Command: `.venv/bin/python tools/calibrate_null.py --outer 40 --inner 19 --bars 252 --output docs/audits/2026-10-23-null-calibration.json`.

The actual engine/comparison/bootstrap pipeline evaluated 40 independent zero-arithmetic-drift GBM price paths, 25% annual volatility, one stock, 252 bars each, with 120 warm-up bars. It selected the higher observed Sharpe edge of SMA trend and RSI reversion, then tested that selected candidate with 19 bootstrap samples and the configured complete training gate. The result records every seed, data digest, selected strategy, edge, DSR, fills, p-value, fallback diagnostic and gate outcome, plus configuration, packages and source identity.

- Null p-value <= 0.05: **0/40**; complete training gate passes: **0/40**.
- Both estimated rates are 0%; both two-sided Wilson 95% intervals are **0%–8.76%**.
- Nineteen inner draws give a coarse minimum p-value of 0.05. This small cohort cannot establish control at 5%, measure power, or validate correlated/staggered histories, short borrow, quick speeds, LLM selection or repeated operator selection.
- Configuration used the default balanced profile and existing costs. No threshold was weakened. The earlier two-run pilot checked runtime/workflow; it is not added to the 40 independent observations.
- **G002 calibration remains partially complete**, with a reproducible first measurement. No general inferential calibration or strategy edge is claimed.

## Final local validation and publication status

```
.venv/bin/pytest -o addopts= -m 'not llm' -q
291 passed, 1 deselected in 630.23s (0:10:30)

.venv/bin/ruff check src tests tools
All checks passed!

.venv/bin/ruff format --check src tests tools
79 files already formatted

python3 tools/collab.py check --base origin/chatgpt/risk-adaptive-20261022
0 errors before the code commit
```

Offline sample import, backtest and ten-draw SMA null test all completed. Observed SMA edge was about 0.17 Sharpe and null p-value **0.182**, indistinguishable from noise; this is a smoke workflow, not the Monte Carlo calibration above. The sample's survivorship and unadjusted-price warnings remain visible.

Code CI: https://github.com/gahitchi/trading/actions/runs/39933309384 . At report drafting, Docker build/container smoke, lint and collaboration checks passed; Python 3.13 and 3.14 jobs were still running. The outbox publication records any later observation. Do not infer completed remote tests from the local pass. Main remains Claude's integration responsibility.

**CI completion update:** the code run above subsequently completed **successfully**, including Docker/container smoke, lint, collaboration, both Python 3.13/3.14 suites and their offline examples. This confirms regression/build checks for the proposed code tree, not resolution of the open qualification findings. Machine-readable local evidence is in `docs/audits/2026-10-23-validation-evidence.json`.
