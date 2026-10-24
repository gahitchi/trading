# Release acceptance protocol — candidate-specific evidence

Status: **not run against IBKR; all release gates remain blocked.** This protocol answers C004 before
collecting data. A green simulator run is not a broker acceptance result. Do not place orders from this
protocol without the operator supervising the paper account. The software continues to refuse real accounts.

## Evidence identity and handling

Freeze the candidate Git SHA, dependency lock hashes, container digest, installed OS package manifest,
configuration hash, strategy source/parameter hashes, calendar version, data subscription/delay, paper
account mode and session time zone before starting. Record UTC receipt times and broker event times
separately. Preserve a hash manifest of original capture files before redaction or normalization.

Raw IBKR captures may contain account identifiers and credentials. **Keep originals in the operator's
access-controlled evidence store, not in Git.** Commit only sanitized evidence, its hash, the private original's
hash, the redaction procedure and an independent reviewer attestation that the same pseudonymous paper
account was used throughout. Never commit passwords, tokens, account numbers, environment dumps, or
unredacted diagnostic bundles. Normalize client-order/trade/account identifiers consistently so relationships
remain testable. Never invent missing reports or infer "no fill" from an empty request.

Every scenario records: candidate SHA, scenario ID, start/end timestamps, source capture hashes, expected
outcome, actual outcome, assertions, reviewer and unresolved exceptions. Missing evidence is **blocked**,
not passed. Unexplained divergence is **failed**. A stopped or incomplete run cannot count as a success.
Reports must distinguish broker observations from induced simulator faults and operator statements.

## Broker acceptance matrix

Run daily/balanced first; only after it passes, run 15m/dynamic with an explicitly validated data-delay
configuration. Start with two or three liquid symbols and an isolated paper account. Choose order sizes
within the configured limits and paper buying power. These tests do not certify a strategy's profitability.

| ID | Trigger and required observations | Pass condition |
|---|---|---|
| B01 | `paper --check-only`, then read-only connect; configured and observed account identity, port, mode, currency and initial cash/positions | Exact paper-account match; supported USD configuration; no orders from preflight; nonmatching/live identity refuses before submission. Negative account tests use the harness, never a real live account. |
| B02 | `paper --smoke --test-order`; complete test/cleanup order statuses, fills and initial/final positions | Every smoke intent has terminal evidence, position delta is exactly zero, no unexplained cash delta after fees; crash/timeout cannot report success. Normal node refuses unresolved smoke state. |
| B03 | Daily decision, next opening auction, each partial fill, commissions and final account values | Each broker trade ID is applied exactly once; sum(signed fills) equals ledger position change to 1e-9 shares; cash delta equals signed proceeds minus actual fees to USD 0.01 after separately classified cash movements. No unexplained residual accepted as rounding. |
| B04 | Interrupt after intent persistence/before acknowledgement; restart, then duplicate execution reports | Same order ID remains attributable; no replacement while status is unknown; replay changes cash/positions zero times for already applied IDs and exactly once for each new fill. Terminal status alone cannot discard cached executions. |
| B05 | Cancel request races with partial/full execution, including a fill delivered after terminal cancellation | Cancel request is not treated as completion. Late fills retain original attribution, no duplicate order/position reversal, unexplained late terminal activity hard-halts for review. |
| B06 | Evidence-based disposal of a truly unreported intent; position mismatch, known partial execution, malformed evidence, delayed reappearance and restart | Only absent, never-booked intents with matching positions and textual evidence can be disposed. Original evidence survives; delayed executions are booked once and halt. Requests for the wrong account refuse. Both live and offline reappearance paths pass. |
| B07 | Competing long/short openings beside unfilled reductions; rejection, partial fills, fee and gap scenarios | Before each submission, every independent fill subset meets configured per-name/gross/short/net/initial-margin limits **after** costs and stated price stresses, plus actual broker buying-power/house-margin checks. A paper order's acceptance alone does not establish this. Current marked-price reservations leave this gate blocked. |
| B08 | Dedicated and allocation account financing; a night with a short/debit balance, fees and an external deposit/withdrawal | Dedicated mode books actual broker charges once; allocation mode explains attributed estimates and their error against broker charges; classified external flows never masquerade as strategy return. Actual borrow/recall and shared-account charge attribution remain unqualified until supplied. |
| B09 | 15m cycle, stale entry cancellation, data outage at exit time, kill-switch halt/flatten, market close and early close | No entries outside the supported session; no duplicate orders while cancellation is pending; quantity exits remain possible without fresh sizing marks, subject to broker/account reconciliation; no assertion of flatness before terminal fills and reconciled zero positions. |
| B10 | Broker daily/intraday bars before normalization, including normal session, DST, holiday and early close | Source event/init timestamps map to documented completed sessions; no future/incomplete bar used; prices/volumes match raw captures. Each missing calendar case stays blocked until observed or independently validated with adapter fixtures. |

A single normal session cannot pass B04–B10. Record which faults actually happened at IBKR; simulator
reproductions are separate supporting evidence. Determine IBKR's supported replay/history horizon from
observed reports plus authoritative adapter/broker documentation. Do not treat the provisional 30 days as
qualified retention. Preserve archived order attribution and associated trade IDs until a reviewed durable
archive/retrieval policy exists. Current JSON archives trade bounded memory for safe retention; their growth
is an explicit operations blocker.

## Remaining release gates: predeclared pass/fail requirements

| Gate | Required evidence and pass rule |
|---|---|
| code-review | All blocking findings answered by ID; Claude reviews the exact candidate SHA and its migration; Python 3.13/3.14, lint, format, offline workflow, Docker/isolation and collaboration CI green. Any unreviewed safety-critical change resets the gate. |
| broker-lifecycle | B01–B09 accepted with raw-origin evidence and both reviewers' decisions; no unresolved cash, position, order-ownership, duplicate or restart discrepancy. |
| research-calibration | Freeze a prospective protocol before running: seed sets, independent evaluation set, correlated/staggered panels, daily/intraday horizons, candidate selection/retry rules, alpha, desired power, uncertainty intervals and sample-size calculation. The full selection procedure (including failed attempts) must pass its predeclared error/power bounds. Neither a small pilot nor a selected strategy's unadjusted p-value passes. No threshold relaxation after seeing results. |
| market-data-and-execution | B07–B10 plus point-in-time membership/source-revision and independently sourced corporate-action, delisting, borrow/recall and execution fixtures. Unsupported economics cannot silently produce qualified results. Sparse next-print replay is not intrabar fill validation. |
| adaptive-product | Implement and qualify strategy eligibility, retirement, fallback, regime changes and manual/supervised/autonomous authority with expiry. Tests must prove no unapproved strategy/parameter/config activation, no future-data selection and deterministic safe fallback. Existing risk presets and continuous walk-forward portfolios alone do not pass. These features remain open, not deferred by this document. |
| operations-and-build | Rebuild from pinned base/Python/build graphs, retain OS package provenance and qualify OS snapshot/Gateway digest. After lifecycle tests, run at least five consecutive US trading sessions including gateway restarts, alert outage/recovery and an overnight hold where applicable. Inject ENOSPC/file+directory-fsync failure, abrupt process death, migration/rollback and supervisor failure in an isolated runtime. Require no lost fills/intents/alerts, no unsafe resumption, no unreconciled ledger change; reconcile each restart. Record RSS, archive/outbox/disk growth and verify configured capacity/exhaustion alarms. Five sessions is a minimum sample, not universal reliability proof. |
| independent-security | A reviewer independent of both implementing agents reviews generated-code isolation, host/container privileges, network/secret access, denial of service and supported architectures. No unresolved critical/high finding; exploit regressions and resource-exhaustion tests pass for the candidate deployment. |

## Release checker usage

`python tools/release_check.py --candidate FULL_SHA` checks all seven mandatory gate IDs. Each passed gate
requires that exact `review_commit`, a named `reviewer`, repository-relative `evidence` paths and an
`evidence_sha256` mapping covering those exact paths. Evidence outside the repository (including escaping
symlinks), modified bytes, missing gates and a dirty worktree fail. This verifies evidence integrity and scope;
it cannot authenticate an operator's observations or replace independent review. Follow-up evidence commits
may refer to a separately identified clean candidate; do not claim tests ran on the evidence-only commit.

Do not flip a gate to passed merely because this protocol exists. Final integration belongs to Claude on
`main`; ChatGPT publishes reviewable changes on its own branch.
