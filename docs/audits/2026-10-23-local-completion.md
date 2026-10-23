# Local completion and external qualification boundary

This is a release candidate for independently reviewed **paper-only** operation.
No gate has been self-certified, no real account was connected, and no real order
was placed during this work. The default product surface remains the CLI.

## Implemented

- Transactional SQLite accounting, active intents and non-expiring indexed trade/order
  attribution. A small schema-3 JSON projection cannot be mistaken for a complete ledger
  by older binaries. The 2 GiB database limit fails closed. Late fills remain attributable.
- Cost and adverse-price reservations, with conservative resizing, used by the same
  execution agent in research and paper. Research identities bind the reservation gap.
  These stress assumptions are not a guarantee against an unbounded market gap.
- Local identity-bound, HMAC-authenticated expiring mandates; autonomous, manual and
  supervised modes; durable, exact, single-use approvals; expiry cancels working orders
  on the heartbeat and blocks normal orders. Existing reconciled emergency exits remain.
- Optional daily strategy-bank eligibility/expiry, fixed-priority regime selection,
  durable exception retirement and cash fallback. The same allocator supports continuous
  backtest evaluation. No automatic promotion, live-money permission, or edge claim.
- Bounded notification admission and observable outage health; failed delivery blocks new
  exposure. Cold private backups, content hashes and a conservative flat-book code upgrade
  requiring fresh external broker evidence. Legacy unbound identities need reviewed migration.
- Python and Gateway image digests, frozen Debian snapshot and exact libseccomp package,
  hash-locked Python/build dependencies; CI tests the image as the unprivileged user.
- Prospectively recorded synthetic calibration runner with immutable seed accounting,
  durable exposure reservations before dispatch, resumable failures and an explicit futility
  rule. Bounded pilots remain diagnostics, not a complete calibration or promotion result.

## Review discoveries resolved during this pass

The first implementation put reservations only in paper execution. The existing
fill-for-fill parity tests exposed the difference. Both backtest and paper now apply
the same reservation policy; the parity regression passes without weakening guards.
The superseded candidate's synthetic run was interrupted, and its 34 completed exposures (and 14 interrupted submissions)
were preserved. They are not presented as validation of the corrected implementation.

Several old assertions assumed a single unchanged entry quantity. Reserves legitimately
permit later top-ups. Borrow fees are now independently reconstructed from fills at
each calendar close, including weekends. The breaker test verifies one breaker,
complete liquidation, no purchases after the breaker and remaining flat through recovery.

## External inputs required

1. Configured IBKR paper account/Gateway and supervised B01–B10 acceptance evidence under
   `docs/paper-acceptance.md`, including order lifecycle, broker buying-power/margin,
   actual cancellation timing and dedicated/allocation account semantics.
2. Independent review of the final candidate by Claude, plus independent security and
   statistical-method review. The current synthetic diagnostics do not qualify the
   complete adaptive policy or the bootstrap null across the research population.
3. The actual deployment host and notification credentials for supervisor, delivery,
   architecture, restoration and prolonged resource-growth/power-loss qualification.
4. Qualified point-in-time market data, corporate-action/delisting and broker borrow/recall
   evidence. Simulation assumptions are still labeled and unsupported cases fail closed.

All seven release gates remain blocked pending the corresponding independent evidence.
Implementation and test evidence are recorded in the adjacent validation JSON and the
repository handoff. No external input was invented to make a gate pass.

## Synthetic diagnostic result

The corrected execution/research candidate's fixed 16-seed power pilot used 199
bootstrap replicates per seed and 504 evaluation bars for two correlated stocks.
The planted predictable regime produced eight full-gate passes out of sixteen;
the descriptive Wilson 95% interval is 0.280–0.720. Eleven p-values met the configured
threshold, but the full gate also checks edge, DSR, signal errors and fills. There
were no execution failures. This does not establish adequate power; it says nothing
about false-positive control on real data or profitability. Thresholds were unchanged.
The much larger predeclared runner remains available, but independent review of the
null/selection/DSR methodology is needed before spending further qualification exposures.

The pilot ran with source digest
`133af35a2df4ff0d4212f68f712b77fe14fa734af0465843585204daa9379ea7`,
unchanged throughout. Subsequent changes validate malformed authority records and strengthen reservation
bounds for independent stock moves and both execution and subsequent mark slippage.
The pilot therefore describes an earlier candidate, not the final execution policy.
Its result supports requiring independent methodology review; it does not qualify
the final candidate. A prospectively agreed study must be run on the reviewed final
implementation before research calibration can pass.

A malformed-authority regression reproduced three uncontrolled type errors. The
fix now returns a controlled refusal; the actual execution heartbeat requests
cancellation and remains observable. Abrupt-process-exit tests also cover both
sides of SQLite commit: cash and trade-ID protection recover together, even when
the diagnostic JSON was not updated.

The independent-price regression reproduces a long/short book that appears neutral
when all marks are stressed together but violates its net floor when the long falls
and the short rises. The corrected bounds cover separate price intervals, both sides
of the equity interval, and adverse fill prices followed by adverse marks. An independent
cash-and-position reconstruction checks all partial-fill/price corners of a mixed book.
Both research and paper use the correction.


The final intraday regression also exposed two assertions requiring exactly one
entry order per day. The tests now permit reserve-driven top-ups while requiring
all entries to precede a single 15:45 liquidation, zero position at each close,
no overnight financing for flat books, and re-entry on every expected session.
All 16 intraday backtest tests pass. This correction changes tests only; runtime
source remains the independently stressed implementation from `70563382`.

## Candidate and validation scope

Review candidate: `1dddadbd1bb45e0a7bb1ce046ab2362e37ca0625` on
`chatgpt/release-completion-20261023`. Its tree is
`d397bf95516d4154c926d07b4350f24f472622ab` and implementation digest is
`3312ff9c8df4b684c9af73988022fc122c3c7feb15f60fc783d8389a8b4f4926`.
The subsequent handoff commit changes documentation and the ChatGPT outbox only.
See `2026-10-23-local-completion-validation.json` for exact commands, results,
evidence hashes, intermediate failures and hosted CI links.

The opt-in LLM test is excluded from the default regression suite and requires
external provider credentials/usage. Real IBKR acceptance and host qualification
were not run. No gate threshold was relaxed, no release was approved, and `main`
was not modified. This candidate requires review of the complete cumulative branch,
including the follow-up parity, malformed-authority and independent-price fixes;
merging the earlier `03e5840` candidate alone is incorrect.

## Final result

Final supported CI: https://github.com/gahitchi/trading/actions/runs/40030701296 — **all six jobs passed** on the exact candidate.

```
pytest (Python 3.13): 430 passed, 1 deselected, 80 warnings in 576.92s
pytest (Python 3.14): 430 passed, 1 deselected, 80 warnings in 577.50s
ruff check src tests tools: All checks passed!
ruff format --check src tests tools: 103 files already formatted
collab.py check: 0 error(s)
```

Docker build, unprivileged isolation/container smoke, compose validation, frozen dependency export checks, wheel/sdist packaging and both offline examples passed. Ten-draw example p=.182 is not calibration or profitability evidence. The incomplete local repeat is not counted as a pass; the final full-suite evidence is hosted CI. Earlier failures and focused results are preserved in the validation JSON. Pandas emits 80 upstream timezone-array deprecation warnings; no test failed.
