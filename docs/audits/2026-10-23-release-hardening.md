# Release hardening review — 23 October 2026

**Final tested candidate:** `57fdc50d9f19b211cc677f428c5f16648ed46575` on `chatgpt/risk-adaptive-20261022`.
**Candidate tree:** `7b1e21696a0718f05306071ed43a674510d746e2`.
**Runtime implementation:** `3187947a656d395dca13fa5669c7f0b5d62d6beb`, tree `da19f9987de69ac891abd49aaf1f119ec826852f`; the final candidate adds review documents and a strengthened account-refusal test, with no runtime change.
**Baseline reviewed:** Claude's `8c10e4c05a6ec282858909c91544dfc52f3be320`, including `d5c9791` and `43046ab`.
**Verdict:** improved development candidate; **not release-qualified**. No real or paper broker connected.
Only Claude can integrate to `main`; this review does not authorize deployment or live trading.

The runtime implementation tree was committed locally as `2f4e56048fbcda006b9c128d78b0ab00242d358f` before publication.
Connector publication changes commit metadata, not file contents. The published tree was independently
compared with the local tree, then local HEAD aligned without changing source files.

## Corrections

| ID | Defect and evidence | Correction |
|---|---|---|
| F1 | Disposal removed an order after its first late partial fill; later fills became foreign. Disposal also erased history after 500 entries and never scanned disposed orders on restart. `tests/test_disposal_review.py` reproduces all three paths. | Retain evidence and intent, continue partial-fill accounting, detect offline reappearance and hard-halt for review. |
| F2 | Cancellation/terminal callbacks deleted the only order attribution; a later execution could no longer update the ledger. | Preserve terminal intents, first replay cached fills, deduplicate later callbacks, recover new offline reports and prevent reuse of any archived ID. Tests cover cancel-before-fill, cached fills and terminal offline replay. |
| F3 | Disposal accepted coerced evidence such as JSON null and could dispose a known partially executed intent. | Strict request types, paper/ledger account checks, known-fill refusal and durable request deletion. The full original evidence remains. |
| F4 | Trade-ID pruning forgot legacy unattributed IDs and disposed-order fills after 30 days, even though disposed intent reactivation could replay them. | Retain IDs belonging to pending, disposed and terminal orders, and all legacy unattributed IDs. Old *unarchived attributed* IDs retain the provisional 30-day policy. Capacity and replay-horizon qualification remain blocked; this is not a bounded archive implementation. |
| F5 | Daily order/turnover caps were saved after submission, leaving a crash window where an order existed but its allowance was not consumed. | Reserve caps before the write-ahead intent; local refusal rolls them back, uncertain transmission keeps them consumed. Regression observes values at the submission boundary. |
| F6 | Malformed ledger fields could be coerced, disposed/terminal state was unvalidated, and JSON duplicate keys/overflowing exponents were accepted. | Validate legacy v1/current v2 state before recovery; reject duplicate trade IDs, wrong types, nonfinite quantities and ambiguous JSON. v2 writes preserve terminal attribution; v1 reads migrate forward. |
| F7 | An in-memory mutation after failed ledger persistence could support later submissions; invalid fill values could contaminate cash. | Disk/serialization failure latches refusal and requests shutdown; invalid/nonfinite or order-mismatched fills halt before accounting. Injected disk/rename faults preserve the prior ledger. This does not prove power-loss recovery. |
| F8 | `abs(NaN - position) > tolerance` is false, so invalid broker positions reconciled; NaN/negative reservation inputs could bypass comparisons. Recovery also ran before ledger-account matching. | Finite position/funding/reservation checks and account gating before fill recovery/terminal processing/adoption. Regression tests refuse wrong/missing accounts without booking their reports. |
| F9 | Two notification workers could select the same last row; after one deleted it, SQLite reused the ID for a new alert, then the other's stale acknowledgement deleted that new alert. A transient SQLite error also killed the durable worker. | Transactional migration to AUTOINCREMENT IDs preserves queued alerts; worker retries database/I/O errors. Delivery is still at least once, so duplicate delivery after a crash remains possible. |
| F10 | Release checker could omit mandatory gates, accept arbitrary review strings, escaping evidence paths and changed evidence bytes. | Require all seven gates, exact candidate SHA, reviewer, confined paths and content hashes. Dirty worktrees fail. This verifies scope/integrity, not authenticity or competence of a reviewer. |
| F11 | Docker base and build backend floated; Python runtime locks did not constrain isolated build dependencies. | Pin the registry-verified Python image digest; pin hatchling and its transitive build graph with hashes; build without dependency-resolving isolation. New package CI checks lock exports and builds both distributions. OS packages are recorded inside the image. OS snapshot, Gateway digest and deployment/architecture qualification still remain open. |

The retained strengths are paper-account enforcement, persistent hard halts, unknown-order conservatism,
independent-fill reservation bounds and the same agent execution path in the live-node harness. No statistical
threshold, reconciliation tolerance, account guard or kill switch was weakened.

## Reproducible regression evidence

Against detached baseline `8c10e4c`, the targeted adversarial selection produced **10 failures, 4 passes,
12 deselections** in 0.22 seconds. Failures demonstrate the pre-submit cap window, NaN reconciliation,
invalid reservations, duplicate/overflow JSON acceptance, notification ID reuse, missing outbox migration
and worker termination. The same tests pass on the candidate. Baseline command:

```sh
PYTHONPATH=/tmp/trading-release-baseline/src .venv/bin/python -m pytest \
  -o pythonpath='' -o addopts="-m 'not llm'" \
  tests/test_review_execution.py tests/test_release_operations.py \
  -k 'daily_caps_are_reserved or nonfinite_broker or invalid_cost or nan_position or ambiguous_or_nonfinite or old_sender_acknowledgement or existing_outbox or transient_outbox' -q
```

The path names are local test setup, not required installation paths. A fresh checkout of the baseline may
be used at any path, while running the candidate regression files with baseline `src` first on PYTHONPATH.

Focused final recovery/reservation/release-evidence/operations selection: **53 passed**. The 16 ledger-schema
cases and existing 30 full-audit cases also passed in focused runs. Build: wheel and sdist pass under the
locked backend. Offline GOOG data/backtest/ten-draw null workflow passes; p=.182 is **not an edge claim**.
Lint, formatting and collaboration structure pass. Final full-suite/CI results are recorded in the accompanying
validation JSON and handoff; do not substitute these focused results for them.

Intermediate runs while source files were still being edited failed the intended runtime-identity check
across harness restarts. They are not green validation. Final validation runs against frozen source, preserving
that guard. No production guard was relaxed to accommodate the development process.

## Outstanding release conditions

`docs/paper-acceptance.md` turns C004 into a predeclared acceptance matrix with ten broker scenarios and
all seven release gates. `RUNBOOK.md` documents the ledger/outbox migrations and rollback hazards. Captures
containing account identifiers/secrets belong in the operator's private evidence store, not in repository history.

Seven gates remain blocked: final independent code review; actual broker lifecycle/buying-power/cash evidence;
prospective selection/false-positive/power calibration; qualified data/corporate-action/delisting/borrow/adapter
and intrabar economics; adaptive strategy-bank/manual/supervised authority features; operations/soak/storage
capacity and migration qualification; independent security review. These include **unfinished product work**,
not only external testing. In particular, terminal-order and trade-ID retention now favors correctness over
bounded JSON growth, and migration does not automatically reset runtime identity. Do not silently remove
these requirements or treat simulator evidence as real-broker qualification.


## Final validation

[CI run 40010796003](https://github.com/gahitchi/trading/actions/runs/40010796003) passed for
`57fdc50d9f19b211cc677f428c5f16648ed46575`:

- Python 3.13: **390 passed, 1 deselected, 80 upstream warnings in 566.82 seconds**.
- Python 3.14: **390 passed, 1 deselected, 80 upstream warnings in 569.26 seconds**.
- Both offline sample/backtest/null workflows passed (p=.182, no alpha claim).
- Docker build, container CLI/configuration smoke, unprivileged isolation, compose validation, locked
  wheel/sdist build, lock-export parity, lint/format and collaboration checks all passed.
- **61 additional regression cases** relative to main's 329-test suite; final focused group **80 passed**.
- `PYTHONPATH=. .venv/bin/python docs/audits/reproduce_recovery_permutations.py`: **1,440 permutations passed**
  across two partial fills, cancellation, duplicate callback, recovery and serialized restart. This exercises
  real execution methods with a test-double broker cache; it is not a LiveNode or IBKR acceptance run.

A stable intermediate full run on the runtime commit had **389 passed, 1 failed, 1 deselected**. The sole
failure expected the old wrong-account diagnostic; the new guard correctly refused earlier. The final
candidate updates that diagnostic assertion and **adds** checks that cash, positions and account identity
remain unchanged. Both complete CI suites then pass. The earlier failure is not reported as green.

Source fingerprint: `32c22af46a7ce0efbf0664c1ce2fb489b97c40d55d7f6c5e63d5f41f670e2b5f`.
The final handoff/evidence commit changes no runtime, dependency, deployment or test files relative to the
tested candidate. Its added standalone evidence script is separately linted/formatted and executed.
Seven release gates remain blocked. Full machine-readable results: `2026-10-23-release-hardening-validation.json`.
