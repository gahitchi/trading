# F1 — Independent economic conservation acceptance packet (Python candidate)

**2026-10-28, owner: ChatGPT, branch:** \`chatgpt/f1-order-reconciliation-completion-20261028\`.
**Disposition:** DEVELOPMENT CANDIDATE; independent review not complete.
**F1 verdict:** BLOCKED. **F0 verdict:** BLOCKED.
**Authorization:** NO exchange session, paper-order authority, live-money authorization, deployment approval, F1 promotion or security waiver.

This packet is a review protocol. Replacing the exact Git SHA, source evidence,
test run, statement scope, acceptance report or independent witness requires a
new review. A green F1 unit-test job alone never proves financial accuracy at
an exchange or broker.

## Supported research-only economics

| Dimension | Explicitly supported and replayed | Rejected rather than silently inferred |
| --- | --- | --- |
| Account / currency | One typed USD cash-account ledger; exact bounded Decimal | FX conversion, foreign currency, cross-currency fees, margin collateral |
| Trades | Source-identified BUY/SELL fills, long/short FIFO in reference ledger, source-bound partial broker executions, explicit USD commission | Claim that simulated fills are real, ambiguous or partial broker account coverage |
| Cash activity | Settled deposits/withdrawals, dividends, interest, withholding, borrow fees/rebates, financing, short distributions | Pending or estimated activity, hidden cash drift, synthetic funding and unsupported classifications |
| Cash corrections | Explicit original-source-bound reversal of one settled CashFact, immutable original and exact opposite postings | Broker trade busts, arbitrary balance write-offs, repeated or partial reversal, original fill rewriting |
| Corporate actions | Exact split/reverse-split share/lot adjustment, bounded fractional cash-in-lieu on independent claimed settled evidence | Delisting, mergers, ambiguous lot entitlements, arbitrary fractional rounding, unreviewed tax and margin treatment |
| Order facts | ACK / rejected / partial fill / cancel request / confirmed cancel, durable source-bytes, receipt-linked economic append | Order submission, native broker auth, replace/expiry not yet modeled, automatic crash recovery trading |
| Independent comparison | Rational cash and positions projection, exact complete external account/order statements and per-receipt identity check | Treating an input fixture as independently verified, externally final, or issuer-attested |
| Persistence | SQLite WAL + FULL, original source-byte custody and hashes, atomic FILL/order write, conflict refusal, idempotent retry, additive v0-to-v1 migration | Signed source origin, externally anchored immutable audit root, throughput guarantee at large N |

Other workstreams remain separate, including F4 order-execution authority, F6
authenticated PAPER integration and the separately developed Rust engine. The
Python model cannot qualify the Rust implementation by implication.

## Exact-candidate reproducibility commands

From a clean checkout of the **reviewed Git commit** on Arch, with frozen deps:

\`\`\`sh
uv sync --frozen --python 3.13 --extra dev
uv run --no-sync pytest -q \
  tests/test_economic_ledger.py \
  tests/test_engine_events.py \
  tests/test_settlement_input.py \
  tests/test_economic_store.py \
  tests/test_account_projection.py \
  tests/test_settlement_semantics.py \
  tests/test_cash_in_lieu.py \
  tests/test_source_binding.py \
  tests/test_account_observation.py \
  tests/test_f1_verify_cli.py \
  tests/test_broker_fill_input.py \
  tests/test_order_observation.py \
  tests/test_order_custody.py \
  tests/test_order_lifecycle.py \
  tests/test_cash_reversal.py \
  tests/test_f1_evidence.py
uvx --from ruff==0.17.3 ruff check \
  src/lab/economic_ledger.py src/lab/engine_events.py \
  src/lab/settlement_input.py src/lab/economic_store.py \
  src/lab/account_projection.py src/lab/f1_evidence.py \
  src/lab/order_lifecycle.py tests/test_f1_evidence.py \
  tests/test_cash_reversal.py
python tools/collab.py check
\`\`\`

The exact CI SHA must pass the complete dedicated F1 workflow for Python
3.13 and 3.14; complete repository CI includes separate, mandatory Docker
security gates. No failure is waived by this packet. Review the F0 two-runner
source/wheel and Arch operator artifact separately.

## Read-only local custody receipt

On a **preexisting** verified operator-supplied journal with no secret values
committed to Git, emit the deterministic F1 manifest (the actual 40-hex SHA is
the reviewed Git commit):

\`\`\`sh
uv run --no-sync python -m lab.f1_evidence \
  --journal ./economic.sqlite3 \
  --account SIM-001 \
  --commit ACTUAL_FULL_40_HEX_REVIEWED_COMMIT > f1-local-evidence.json
\`\`\`

The local receipt reports exact canonical economic and order evidence heads,
source inventory digest, full archived order observation checks, confirmed
logical vs missing order receipts and an independent rational arithmetic
comparison. It does **not** sign itself. All origin, external anchoring,
review and execution authorization flags are false by construction.

For actual independent account/order observations at the same snapshot
boundary, invoke:

\`\`\`sh
uv run --no-sync python -m lab.f1_verify \
  --journal ./economic.sqlite3 --account SIM-001 \
  --statement ./independent-account.json \
  --orders ./independent-orders.json \
  --source broker-export --require-order-lifecycle
\`\`\`

The account and order statement names above are illustrative; do not invent
records to produce a PASS. No statement contents, credentials, private account
identifiers, or raw brokerage documents should be committed or uploaded
publicly. Expose only reviewed digests and redacted acceptance metadata.

## External acceptance evidence that still must be supplied

1. **Independent F0 baseline** — reviewed exact commit, vulnerability-free
   mandatory container security scan, reproducible build receipts, Arch
   operator receipt and independent source/sign-off.
2. **Authenticated external source binding** — approved broker adapter/export,
   actual account identity and scope, documented execution IDs and complete
   order/account snapshots with authentication/finality evidence. The local
   JSON parsers validate structure and byte equality, **not** the source.
3. **Externally anchored original evidence** — independently stored SHA-256
   source/order/journal roots and an immutable or signed witness record.
4. **Independent economic oracle** — independently computed USD cash, FIFO
   inventory, fees, positions, splits and supported corrections on original
   source transaction IDs. A rational projector over our own input is
   mathematical independence only, not independent source collection.
5. **Arch operator failure exercises** — clean start, crash-before/after-
   commit, restart/replay, migration/backup/rollback, stale/duplicate
   source receipt, partial fill/cancel race and read-only reconciliation.
6. **Performance acceptance** — documented size/latency/memory envelope on
   the intended hardware, including long-history replay. The Python
   append/replay model remains superlinear; no high-throughput qualification
   can be assumed from functional unit tests.
7. **Reviewer decision** — exact candidate SHA, workflow run IDs,
   vulnerability disposition, independent check scripts/outputs, supported
   economic scope, explicit exclusions, findings and acceptance signature.
   Only after F0 is accepted may F1 be independently qualified.

## Internal implementation constraints for review

- Cash reversal is intentionally limited to previously settled CashFact.
  Trade bust/replace and corporate-action compensation remain unsupported
  economic types, **not** TODOs that can be silently substituted. A future
  broker profile requiring these cases is out of the current supported scope.
- The current order-observation report has a bounded per-report count.
  The replay-first economic journal is not suitable as an unbounded
  high-frequency production ledger without performance redesign.
- No automatic matching report may overwrite the Trading Lab economic book.
  All residual cash/position differences must remain explicit mismatches.
- Python and Rust branches have distinct authorities and test requirements;
  an independent reviewer must compare semantics at the exact submitted SHA.

**Review instruction:** classify each open item as external evidence,
unsupported excluded behavior or genuine implementation defect. Any genuine
defect requires a code change and rerun; do not mark F1 complete from this
packet alone.


## Deterministic Arch replay-performance probe

Run the synthetic append-and-cold-restart probe on the same reviewed Python
candidate. Measure representative batch sizes; record exact hardware,
Python/SQLite versions, process isolation, input count, p50/p99 from repeated
independent trials and operator-defined latency/memory budget. The utility
measures total bounded append duration and cold replay, and verifies the
original generated economic book against a full read-only rebuild.

\`\`\`sh
uv run --no-sync python -m lab.f1_benchmark --events 64
uv run --no-sync python -m lab.f1_benchmark --events 256 \
  --max-append-ms OPERATOR_SELECTED_LIMIT \
  --max-cold-replay-ms OPERATOR_SELECTED_LIMIT
\`\`\`

When no budgets are supplied, the result's \`selected_limits_passed\`
is null, **not** true. If a selected budget is exceeded the command exits 2.
No source authentication, real hardware qualification or independent signoff
is inferred from synthetic timings. This probe does not measure memory
consumption or all broker-order-history replay paths; those require
independent operator instrumentation and review. Full-replay append remains
superlinear and may need redesign before continuous high-throughput use.
