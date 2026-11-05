# Crash research: open-interest quantity correction

## Root cause and data semantics

The upstream Binance daily metrics CSV provides both:
- `sum_open_interest`: outstanding open-interest contract/base quantity.
- `sum_open_interest_value`: open-interest *notional* in USDT.

The previous crash detector used the USDT notional to decide whether open
interest was increasing or decreasing during a price crash. That is
incorrect: notional OI can fall as price drops even when contracts increase.

This maintenance-branch implementation stores both `oi_qty` and
`oi_value`. The crash detector consumes **only `oi_qty`**, while keeping
the existing five-minute publication delay and 15-minute staleness rule.
Invalid, notional-only or mixed-version OI caches fail closed.

The existing `lab crash study` command was ported onto this branch to
allow independent qualification of the correction, **not** to authorize a
holdout test or declare a profitable short strategy.

## Existing Parquet cache migration

Do not delete or overwrite the existing research data preemptively. Never
run the migration on the reserved holdout. The example below uses the
previously reserved **2026-05-08 UTC** partition. Check the partition
stored in the actual research checkout before using any value in commands.

From the validation branch's clean worktree with the Debian venv active,
an audit-only dry run can inspect the *historical research* cache in the
original Termux checkout:

    PYTHONPATH=src:. python - <<'PY'
    import json
    from pathlib import Path
    import pandas as pd
    from lab import crash_data as cd
    from lab.config import load_config

    original = Path.home() / "trading-termux-tests"
    cfg = load_config(original / "lab.toml")
    partition = cfg.results_dir / "crash_partition.json"
    if not partition.is_file():
        raise SystemExit("BLOCKED: original crash holdout partition is missing")
    boundary = pd.Timestamp(json.loads(partition.read_text())["holdout_start"])
    if boundary.tzinfo is None or boundary.utcoffset().total_seconds() != 0:
        raise SystemExit("BLOCKED: partition is not a UTC timestamp")
    if boundary.time() != pd.Timestamp("00:00").time():
        raise SystemExit("BLOCKED: partition is not at a UTC day boundary")
    print("Protected holdout starts:", boundary)
    pending = cd.migrate_oi_quantity(
        cfg.data_dir / "crash", "BTCUSDT", before=boundary.date()
    )
    print("Legacy pre-boundary OI files:", len(pending))
    print("First 5:", pending[:5])
    PY

The default is **dry run**: no archive download or Parquet rewrite.
The cutoff is read from the original research checkout, not inferred from
current dates or validation-worktree results. Use the original historical
cache; do not create a new partition.

### Controlled migration CLI (preferred over direct migrate_oi_quantity)

Use the **new** guarded migration tool; it never defaults to modifying
historical data and does not accept a hand-entered cutoff. It reads the
existing partition from the specified **original** lab.toml.

First run the offline transaction tests, then inspect the exact two selected
days in a read-only dry run:

    PYTHONPATH=src:. python -m pytest       tests/test_crash_oi_migration.py -v

    PYTHONPATH=src:. python -m lab.crash_oi_migration       --config ~/trading-termux-tests/lab.toml       --symbol BTCUSDT --limit 2

Only after the dry-run selection, backup destination, filesystem headroom
and original research partition are verified, run a **pilot** migration:

    PYTHONPATH=src:. python -m lab.crash_oi_migration       --config ~/trading-termux-tests/lab.toml       --symbol BTCUSDT --limit 2 --apply

On each processed historical day the tool:

1. Saves the **exact raw Parquet bytes** to
   `data/crash/oi-original-backups/BTCUSDT/<day>.parquet`, verifies SHA-256
   and fsyncs the immutable copy.
2. Downloads the original Binance daily metrics ZIP and validates every
   timestamp and **exact** USDT notional value against the legacy Parquet.
3. Writes and checks an upgraded temporary Parquet, recording the original,
   ZIP and new-file SHA-256 hashes in a durable `.migration.json` transaction.
4. Atomically replaces only that pre-holdout file and commits the transaction.
   On each later --apply invocation, all prior transactions are verified and
   prepared manifests are reconciled **before** the next legacy files are
   selected. This catches a kill after the data rename when the upgraded
   Parquet no longer appears in the legacy-file inventory.

Migrations are limited to at most **20 days per invocation**; the default is
two. A 128 MiB minimum free-space reserve and a directory lock are enforced.
A mismatch aborts the affected day without overwriting its legacy data.
Existing repaired days are not downloaded again.

These backup copies are **on the same device**, and cannot protect against
physical device loss; preserve a separate off-device copy of valuable raw
history. Do not run other crash-research or data-fetch processes while the
cache is being migrated. Historical market research remains unqualified
until all eligible pre-holdout OI days have been corrected and audited.

The first two-file application is a pilot, not permission to start the
remaining 856 days unattended. After that pilot, separately compare the
backup-file and original SHA-256 hashes from each transaction sidecar and
verify `oi_qty` in the upgraded Parquet, before increasing batch size.

The original unbounded Python `migrate_oi_quantity(..., apply=True)` API
exists for backwards compatibility, but should **not** be used for this
858-file operational migration because it does not retain per-day backups.

Research-only crash studies use pre-boundary OI periods and never open
post-boundary OI cache files. A *future* authorized holdout evaluation
must first verify the held-out OI data's quantity representation, but
must never be used to fit thresholds.


### Independent read-only audit after every batch

The audit command verifies **every** committed transaction, not just the
latest batch. It checks original and upgraded SHA-256 digests, byte-backed
Parquet values and timestamps, quantity positivity, record counts, migration
status and holdout boundaries. Orphan backups and incomplete transactions
cause a nonzero exit code. It does not open holdout Parquet files or mutate
any cache, manifest or lock file.

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_oi_audit.py -v

    PYTHONPATH=src:. python -m lab.crash_oi_audit \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT

Only continue if the JSON report has "ok": true, "issues": [] and the
expected number of verified historical files.

Once a first 20-file batch and its independent audit have passed, a bounded
five-batch pass can be run explicitly from the validation worktree:

    (
      set -e
      for batch in 1 2 3 4 5; do
        echo "OI migration batch $batch / 5"
        PYTHONPATH=src:. python -m lab.crash_oi_migration \
          --config ~/trading-termux-tests/lab.toml \
          --symbol BTCUSDT --limit 20 --apply
        PYTHONPATH=src:. python -m lab.crash_oi_audit \
          --config ~/trading-termux-tests/lab.toml \
          --symbol BTCUSDT
      done
    )

Any migration or audit failure aborts the subshell immediately, retaining all
previously committed days and their backups. A later batch invocation can
resume, after the failure is diagnosed. **Do not** run this alongside another
migration or crash study. Full migration of all days requires subsequent
explicit passes; do not run unbounded unattended loops.


### Five-minute coverage diagnosis (different from migration integrity)

The migration audit proves that upgraded Parquet content matches the original
archive for timestamps and OI notional. **It does not establish the original
Binance archive contained every five-minute snapshot.**

A read-only coverage report checks **already migrated pre-holdout days**
against a benchmark of 288 five-minute observations per UTC day. It compares
sorted unique timestamps against both five-minute grids (period-start
00:00–23:55, period-end 00:05–next 00:00) and distinguishes missing slots,
duplicates and off-grid timestamps. Out-of-order archived rows are reported
separately as `ordering_only_days` and are advisory if all unique timestamps
exist. The crash research loader sorts OI timestamps before aligning signals.
The checker never opens protected holdout files.

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_oi_coverage.py -v

    PYTHONPATH=src:. python -m lab.crash_oi_coverage \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT

Missing, duplicate or off-grid timestamps intentionally give a **nonzero exit**.
Shuffled archives with an otherwise complete, unique timestamp grid do not
alone fail the gate. Coverage is separate from migration integrity; incomplete
source history does not imply migration corruption.
Continue to use `lab.crash_oi_audit` for post-batch migration integrity.
The coverage report has \`integrity_verified: false\` because it does not
repeat SHA-256 verification. Run both audits separately.

The 2024-01-01 through 2024-05-01 migration produced 35,011 observations
across 122 days, compared with a 35,136-observation full-day benchmark.
The 125-observation discrepancy was localized to **2024-02-16**: 163
observations from 00:00 through 13:30 UTC, apparently missing 13:35–23:55.
April 2024 also contained shuffled timestamp rows. The revised sorted-grid
report is needed to determine whether April dates are merely shuffled or
have duplicate/missing timestamps. No gaps may be filled with invented values.


### Paused historical batch: original Binance OI contains zero or negative data

On 2026-11-04 a 20-day BTCUSDT migration batch starting 2024-07-01
stopped on the source parser's positivity check. The *previous* 182 files
were verified through 2024-06-30. The failed invocation may have committed
some following days before encountering the invalid source, since each
file is independently transactional.

Before any retry, use the existing read-only integrity audit and dry run to
establish the exact current state. The failed day may retain a byte-perfect
backup without a committed manifest; the integrity audit intentionally
reports that as an orphan backup and exits nonzero:

    PYTHONPATH=src:. python -m lab.crash_oi_audit \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT

    PYTHONPATH=src:. python -m lab.crash_oi_migration \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT --limit 2

The new **read-only** forensics tool retrieves only the first still-legacy
pre-holdout archive ZIP into memory. It never writes or modifies the cache,
and it reports offending row timestamps and the exact offending field:

    PYTHONPATH=src:. python -m lab.crash_oi_forensics \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT

This diagnostic emits a nonzero exit code when the selected source is
not eligible for migration. It also reports the source ZIP's SHA-256, the
legacy file's SHA-256, and whether timestamps and original OI notionals
match. If zero or negative quantities/notionals are present, do not replace
them with guessed values, relax parser validation, or delete a failed-day
backup. Do not run the rest of the migration until the cause is investigated
and a documented handling rule is tested. The protected holdout is not read.



### 2024-07-09 BTCUSDT: exact-source zero-pair quarantine

The original Binance daily metrics ZIP at
\`data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-07-09.zip\`
contains six **(quantity=0, notional=0)** pairs at 17:25, 18:05, 19:00,
19:15, 20:20 and 20:35 UTC. All 288 timestamps and the original
notional values match the cached file. These six OI values are not
credible evidence of the exchange's actual outstanding positions.

**This does not authorize replacing zeros with interpolated values.**
The isolated exception path converts the six unusable *quantity* values
to NaN and attaches an explicit \`oi_valid=False\` marker; it preserves
the original zero *notional* values and all timestamps unchanged.
Regular ingestion and general batch migration continue rejecting zero
quantity. Research must veto signals for any OI lookback spanning a
published invalid snapshot. The quality-aware coverage checker reports
the six unavailable observations separately from missing timestamps.

Original ZIP SHA-256 (from the operator's read-only forensic report):

    7954ac8410a251604524ba8398f0753332ce2542bacadbdb4c5a5a77bb585150

Original legacy Parquet SHA-256 (same verified report):

    2d8d3b30b47a708cb3354ae1c92a94a1dfd03de18859a796c2eb789a64bb8748

The failed attempt created a byte-exact backup without a completed
manifest. That orphan is intentional and must match the pinned legacy
digest; do not delete it.

Validate the new exception path and all existing regression tests first:

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_oi_quarantine.py \
      tests/test_crash_oi_forensics.py \
      tests/test_crash_oi_coverage.py \
      tests/test_crash_oi_audit.py \
      tests/test_crash_oi_migration.py \
      tests/test_crash_oi.py \
      tests/test_crash.py \
      -k "not test_cli_research" -v

Then inspect the exact day **without --apply**:

    PYTHONPATH=src:. python -m lab.crash_oi_quarantine \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT --day 2024-07-09 \
      --archive-sha256 7954ac8410a251604524ba8398f0753332ce2542bacadbdb4c5a5a77bb585150 \
      --legacy-sha256 2d8d3b30b47a708cb3354ae1c92a94a1dfd03de18859a796c2eb789a64bb8748 \
      --expect-zero-pairs 6

That read-only command must return \`eligible_for_explicit_quarantine=true\`;
it checks both content hashes, every archive timestamp and notional, the
exact six (0,0) pairs, and the quality-mask result. Do not apply the
exception until the tests and dry-run are accepted.

With explicit operator approval, the *same command* plus \`--apply\`
atomically migrates only that named pre-holdout day, preserving the
original backup and recording the six invalid timestamps in the
transaction. Follow with the independent \`lab.crash_oi_audit\` before
any further ordinary migration. A successful repair should produce
191 committed files and 667 remaining, unless unrelated files have
changed. \`lab.crash_oi_coverage\` should still flag 2024-02-16's
125 missing slots and the six quarantined 2024-07-09 measurements.
This is not a qualified trading strategy or permission to read holdout.


### Six-day July 10–15 BTCUSDT source-data anomaly

After successfully quarantining July 9, the next default migration stopped at
July 10. The read-only forensic scan established the following exact counts of
nonpositive quantity and notional snapshots, with identical archive and local
timestamp/notional series:

| Day | Suspect snapshots / 288 |
| --- | ---: |
| 2024-07-10 | 41 |
| 2024-07-11 | 46 |
| 2024-07-12 | 43 |
| 2024-07-13 | 52 |
| 2024-07-14 | 85 |
| 2024-07-15 | 26 |
| **Total** | **293 / 1,728** |

July 16–19 had no nonpositive values in the same forensic check.
The six days contain 293 suspect samples, a particularly poor-quality
OI interval; **quarantine is not a finding that OI signals are usable
throughout these dates**. The research mask must continue vetoing OI
lookbacks spanning unavailable observations, and experiments must report
reduced eligible-signal coverage. Never interpolate quantities or drop
raw rows. Keep original backups and notional zeros.

Their 12-sample forensic previews cannot alone prove all 293 are exact
(0,0) pairs. **The preflight parser checks every row** and rejects any
negative, nonfinite or asymmetric zero pair. The recorded original ZIP
and legacy Parquet hashes are pinned for every day in:

    docs/crash-oi-quarantine-2024-07-10-to-15.json

The independent audit after the stopped July 10 attempt reports the
expected orphan original backup, while retaining 191 verified files and
667 legacy files remaining. This must not be "fixed" by deleting a backup.

Use the following **read-only** preflight after fetching the maintenance
branch (no cache, lock, backup or manifest changes):

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_oi_quarantine_batch.py \
      tests/test_crash_oi_quarantine.py \
      tests/test_crash_oi_audit.py \
      tests/test_crash_oi_migration.py \
      tests/test_crash_oi_coverage.py -v

    PYTHONPATH=src:. python -m lab.crash_oi_quarantine_batch \
      --config ~/trading-termux-tests/lab.toml \
      --manifest docs/crash-oi-quarantine-2024-07-10-to-15.json

The preflight requires \`preflight_ok=true\`, \`checked_days=6\`,
\`total_zero_pairs=293\` and six individual matching pinned hashes.
If any fail, no day is written.

**Only following operator review**, the same invocation with \`--apply\`
migrates the six days individually, using the pinned ZIP and original
Parquet hash on every application and running the full independent
migration integrity auditor **after each committed day**.
An error stops the batch, reports the failing day and already committed
prefix. The six-day operation is not atomic.

    PYTHONPATH=src:. python -m lab.crash_oi_quarantine_batch \
      --config ~/trading-termux-tests/lab.toml \
      --manifest docs/crash-oi-quarantine-2024-07-10-to-15.json \
      --apply

Following six successful commits, the expected result is
\`verified_files=197\`, \`verified_records=56611\`,
\`legacy_remaining=661\`, \`quarantined_zero_pairs=293\`,
and \`migration_integrity_ok=true\`.
Afterwards, run \`lab.crash_oi_audit\` again as an external acceptance check.
The independent coverage audit must still flag February 16's
125 missing timestamps and the 299 quarantined zero-pair snapshots
(July 9's 6 plus July 10–15's 293). It will not return a clean
data-quality status merely because checksum integrity passes.


### 2024-08-12 migration stop: read-only source survey

The normal migration resumed successfully through 2024-08-04 with 217
independently audited files, then committed seven more days through
2024-08-11 before stopping on a nonpositive source OI value for
2024-08-12. The state is at least 217 audited files; the next audit
should confirm 224 committed files (and may correctly report an orphan
backup for the failed August 12 attempt). Do not delete the original
backup or repeat a batch until the anomaly is characterized.

Because July exhibited clusters of exact (0 quantity, 0 notional) pairs,
use the **read-only**, date-bounded OI archive survey:

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_oi_source_survey.py \
      tests/test_crash_oi_quarantine_batch.py \
      tests/test_crash_oi_quarantine.py \
      tests/test_crash_oi_migration.py \
      tests/test_crash_oi_audit.py \
      tests/test_crash_oi_coverage.py -v

    PYTHONPATH=src:. python -m lab.crash_oi_audit \
      --config ~/trading-termux-tests/lab.toml --symbol BTCUSDT

    PYTHONPATH=src:. python -m lab.crash_oi_source_survey \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT --start 2024-08-12 --days 14

Default survey performs no writes, locks or backups. Each selected
pre-holdout day is checked against its exact original Binance ZIP:
timestamps, notionals, exact ZIP/local SHA-256 digests, quantity and
notional finiteness, and aligned zero-pair masks. Only exact (0,0)
pairs may become candidate exceptions; unexpected negatives, mixed
zeros, corrupt archives, already-migrated days or missing files are
reported as \`blocked\`. It cannot read a range reaching the holdout.
At most 20 calendar days are scanned.

Results categorize each day as \`clean\`, \`exact_zero_pairs\` or
\`blocked\`. This is **not a migration command**; it cannot authorize
exceptions. If a short, consecutive range contains only exact zero-pair
days, rescan just that range (up to six days) with explicit
\`--manifest-out /tmp/august-oi-review.json\` to create a **new** JSON
candidate plan without touching market data. The command refuses to
overwrite an existing path. Separately validate the generated plan
through \`lab.crash_oi_quarantine_batch\` **without --apply** and
review the result before considering any write operation.

Never assume that the 2024-08-12 anomaly matches July's source quality.
Do not globally relax the OI positivity constraint or silently
interpret archived (0,0) values as genuine market-wide open interest.


### Short-source-gap signal gate: 2024-10-28 audit

A later independent coverage check identified **127 missing five-minute
source rows** across 433 migrated days, exactly:
- 2024-02-16 13:35–23:55 UTC: 125 contiguous absent timestamps;
- 2024-10-28 16:25–16:30 UTC: 2 contiguous absent timestamps.

This is *in addition to* 311 explicitly quarantined (0,0) OI rows
through 2025-03-08. The checksum/transaction audit remains clean: a
correctly preserved source gap is not an integrity-failure claim.

The crash strategy formerly used a 15-minute forward-fill staleness
limit and explicitly vetoed quarantined zero-pair publication events,
but a **short missing archive interval** could still produce valid
endpoint values and create a false OI-change signal. It now marks each
absent expected five-minute source timestamp as unavailable starting
at the source timestamp + the configured five-minute publication delay.
Its signal gating then vetoes any crash trigger whose OI comparison
window includes that absence. No interpolation, substitution or price
lookahead is permitted; the source gap and raw files remain unchanged.
Historical coverage is still *not complete*, and eligible-signal
statistics must reflect these excluded windows.

Test independently:

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_oi_missing_gap_signals.py \
      tests/test_crash_oi_quarantine.py \
      tests/test_crash_oi_coverage.py -v

These tests cover the two-row October gap, 125-row February gap,
quarantine-plus-gap conjunction, complete OI cadence and the
short-side breakdown lookback veto before/after the gap.

## BTCUSDT pre-holdout migration completed (Termux acceptance, 2026-11-05)

Operator-run migration and independent read-only audit output establish this
**dataset migration checkpoint** (not strategy qualification):

- Scope: all 858 daily BTCUSDT OI Parquet caches, 2024-01-01 through
  2026-05-07 UTC; protected holdout starts 2026-05-08 UTC.
- The migration auditor: ok=true, verified_files=858,
  verified_records=246974, legacy_remaining=0, issues=[].
- The coverage auditor: checked_days=858,
  full_day_count_benchmark=247104, observations=246974,
  count_shortfall=130, errors=[].
- Missing source timestamps (not filled):
  - 2024-02-16 13:35–23:55 UTC: 125;
  - 2024-10-28 16:25–16:30 UTC: 2;
  - 2025-08-29 06:20–06:30 UTC: 3.
- A separate 330 source (0,0) OI rows were explicitly quarantined;
  these remain timestamped but unavailable for quantity-based signals.
- The full-period OI cadence/quality report intentionally remains
  coverage_ok_for_checked_days=false: incomplete source coverage and
  quarantined observations are reported, not silently accepted as valid.
  The coverage checker does not independently certify transactional
  integrity; use the two auditors together.

These figures are taken from the external Termux acceptance transcript;
they are not a claim that GitHub CI had access to the operator's dataset.
Do not edit, interpolate, backfill, or inspect the protected holdout to
make coverage appear complete.

**Next qualification gates:** re-run behavioral regression tests;
audit minute-bar availability and causal time alignment; quantify eligible
signal windows excluded by gaps and quarantines; freeze an immutable
content/provenance fingerprint; stress taker costs and realistic short-side
execution; complete trial accounting and prospective statistical
qualification. Holdout studies and releases remain disabled. Historical
migration completion is not evidence of profitability.

## Crash input fingerprint qualification (2026-11-05, pending Termux regression)

Research-mode crash evaluation now excludes candle **and** open-interest
archives overlapping the protected holdout before opening files. It also
drops any period-end records stamped at or beyond the reserved boundary.
A crossing monthly candle archive may therefore conservatively omit
usable pre-boundary data; it must not be opened just to recover those rows.

The durable crash-study attempt now receives two canonical SHA-256 input
streams per requested symbol: the UTC-sorted minute candle frame and
five-minute open-interest frame after boundary filtering, including
schema, typed numeric values, explicit null/quarantine values, timestamps,
and absent-input markers. Each digest is appended to the attempt under
its existing trial evidence model; failed attempts retain the partial
or complete evidence already gathered. Code identity is separately
recorded in the trial ledger. Source-frame fingerprints distinguish
different inputs; they are **not** standalone proof that every
downstream statistic is statistically valid.

The research-mode quality policy is versioned to include the
missing-five-minute-snapshot veto. Holdout tests and release remain
disabled. This code is awaiting independent external Termux regression;
do not interpret it as qualified before those tests pass.

Run:

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_input_provenance.py \
      tests/test_crash.py tests/test_crash_oi.py \
      tests/test_crash_oi_missing_gap_signals.py -v

## Read-only pre-holdout signal-window quality diagnostic (pending external tests)

The new command `lab.crash_quality_gate` reads only cached pre-holdout
candle and OI data. It refuses unsafe symlinked cache files and malformed
minute OHLC/volume measurements, and reports:

- Missing one-minute candle timestamps within the loaded candle range,
  with bounded contiguous-gap examples and a trailing interval to the
  reserved holdout boundary (important if a crossing monthly archive is
  excluded).
- Invalid 5-minute OI *source* observations separated into absent
  snapshots and explicitly quarantined (0,0) records, by their earliest
  conservatively assumed +5-minute publication time.
- Potential decision minutes whose price, volume warmup, and OI endpoints
  are available **before and after** the independent OI quality mask.
  Quarantine and absent-source overlap is reported without double-counting
  the combined excluded windows.
- Hypothetical taker round-trip 20/30/50 bps cost cases (5 bps per-side
  fees plus 5/10/20 bps per-side slippage). These are *assumptions*,
  not measured fill costs, funding, trade P&L or parameterized strategy
  performance.

This diagnostic never evaluates a price-drop threshold, emits trades,
edits cached inputs, registers a strategy trial, tests the holdout or
claims predictive profitability. A full crash strategy run must still
reserve a trial before reading data. It is a quality/eligibility
diagnostic, not a parameter-search or research approval tool.

Run from the original research checkout:

    PYTHONPATH=src:. python -m lab.crash_quality_gate \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT --window-min 60

Verify before trusting the report:

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_quality_gate.py \
      tests/test_crash_oi_missing_gap_signals.py \
      tests/test_crash.py -v

No external Termux acceptance has yet been received for this change.

## Daily candle tail qualification (accepted on Termux, 2026-11-05)

Operator-run Termux acceptance passed **50/50 tests** covering daily
archive source pinning, pre-access holdout exclusion, and read-only
quality reporting. A read-only preflight confirmed 7/7 original daily
BTCUSDT Binance 1m ZIPs for **2026-05-01 through 2026-05-07**, each
containing exactly 1,440 ordered one-minute candles. The source plan
pins the original ZIP SHA-256 for every day.

The append-only apply committed 7/7 daily Parquets and preserved the
excluded, holdout-overlapping monthly archive. Independent read-only
audit returned `ok=true`, `checked_days=7`, and `issues=[]`.

The repeated quality audit after recovery found:

- Holdout boundary: **2026-05-08T00:00:00+00:00** (unchanged).
- Candles: **1,235,519** loaded pre-holdout minutes, from
  2024-01-01T00:01Z to 2026-05-07T23:59Z; **0** missing minutes
  within the loaded interval; **0** trailing gap minutes.
- Exactly **1** boundary-stamped period-end source candle excluded.
- OI: **246,974** five-minute records; **130** source timestamps
  missing; **330** explicit (0,0) quarantine records.
- At the 60-minute OI comparison window, **1,230,318** candidate
  decision minutes before the independent OI quality veto,
  **5,069** excluded (144 missing-source, 4,925 quarantine,
  0 overlapping reasons), and **1,225,249** still available.
  These are *data-ready minute windows*, **not** triggered crash
  signals, trades, performance or predictive validity.
- The separate OI migration audit had already verified all **858**
  historical pre-holdout Parquets with 0 legacy remaining and 0
  transaction issues.

The 20/30/50 bps taker round-trip scenarios remain modeled
assumptions, not measured market impact, funding or P&L. No
holdout research was authorized or run. All observations above are
taken from operator-provided Termux outputs, not CI access to
operator-local market data. Full strategy cost, funding and
statistical qualification remain pending.

## Crash short-side execution assumption upgrade (external tests pending)

Exploratory crash trades now retain both reference prices and calculate
side-aware entry/exit taker fills. For a fixed BASE quantity, assumed
adverse slippage changes **both** entry and exit prices; the fee applies
to each *executed* notional, not a flat 2 x fee subtraction from initial
notional. Long and short sides share the same explicit fill formula.

A research attempt's parameters now identify the revised model as
`reference-taker-fixed-base-two-fill-notional-fees-v1`. The same
already-generated trade signals, entries, exits, and weights are then
repriced using three **fixed** stress assumptions (fee 5 bps each
side, with slippage 5 / 10 / 20 bps each side), corresponding to
20 / 30 / 50 bps at unchanged price. The stress results are attached
to the **same reserved trial** alongside SHA-256 input fingerprints.
They do not silently add optimised strategies or bypass trial logging.
Fee costs now reflect both executed prices and therefore differ slightly
from a nominal flat-round-trip rate on large moves.

This remains a **reference-price taker approximation**: slippage is
assumed rather than empirically measured, and it has no funding,
exchange mark-to-market settlement, spread calibration, partial fills,
queue estimates, market impact, liquidation, borrow/margin interest,
or exchange-specific tiered commission schedule. A positive return
under any scenario would *not* qualify profitability. In particular,
short funding may be positive or negative; it must not be assumed to
benefit the strategy.

Regression command:

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_execution.py tests/test_crash.py \
      tests/test_crash_input_provenance.py -v

**External Termux test acceptance is pending.** Existing historical
strategy results used an older cost model and may not be compared as
if their results used identical execution assumptions. Protected
holdout testing and release remain blocked.

## BTCUSDT funding source survey accepted on Termux (2026-11-05)

Operator acceptance of exact-millisecond funding validator v2:

- The offline crash regression suite completed with **86 passed**.
- The real BTCUSDT historical public funding survey returned
  **2,574** events across **3** Binance USD-M API pages
  (1,000 / 1,000 / 574), bounded from 2024-01-01 UTC to
  strictly before the protected 2026-05-08 UTC holdout.
- The source-plan kind is
  `binance-usdm-funding-history-preholdout-v2`.
- Saved event digest:
  `dc2d1b10da620664285fd0b25a45a1522214bf8af52d257ab36e2bc348eb2a8c`.
- Page SHA-256 fingerprints and exact page cursors are present in the
  operator-held plan:
  `~/trading-termux-tests/btc-funding-preholdout-plan.json`.
- Three settlement events per 858 research days is consistent with the
  count, but cannot by itself establish correct rate, mark-price, and
  event-time assignments.

**Next gate:** apply the saved plan only via the source-hash-verified
`lab.crash_funding --apply --plan ...` path, followed by the
independent read-only `--audit --plan ...` check. Neither step
enables funding-adjusted crash research automatically. Do not claim
strategy P&L incorporates funding until the trade ledger explicitly
joins accepted settlement evidence. Existing OI and candle caches and
the protected holdout must remain unchanged.

## Historical funding timestamp validator correction (2026-11-05)

The initial live BTCUSDT funding survey failed before creating its
source plan. The prior validator incorrectly required Binance API
`fundingTime` to be divisible by 60,000 milliseconds. Funding history
reports **exact event milliseconds**; the API does not promise exact
UTC-minute alignment. The original combined error could also mean an
out-of-bound event, so the actual cause is **not yet established**.

The v2 source validation now:
- Preserves raw millisecond funding timestamps and pins them unchanged.
- Separately rejects events outside the explicit [start, holdout) bounds.
- Accepts only events within 30 seconds of a UTC clock hour for this
  BTCUSDT research protocol. Events beyond that tolerance fail with an
  exact timestamp for investigation; no rounding or interpolation occurs.
- Allows up to 30 seconds of source clock jitter on the conservative
  eight-hour coverage check. It still rejects source gaps and missing
  event evidence.
- Treats funding events within 30 seconds of a modeled trade's entry or
  exit timestamp as ambiguous, disallowing false settlement attribution.

All plans now use version 2 so historical v1 plans cannot be accepted
as equivalent provenance. This is a boundary/clock-validation correction,
**not evidence that any actual funding survey has passed**. The exact
root cause must be confirmed by the next read-only Termux preflight.
The on-disk OI/candle data and holdout are unaffected.

## Pre-holdout funding history qualification (pending external acceptance)

The USD-M perpetual short strategy needs settlement-time funding payments.
A positive Binance funding rate means the short side receives funding;
a negative rate means the short side pays. A fixed-base contract quantity
earns -side * rate * settlement_mark / entry_reference per event.
We cannot substitute 1-minute trade closes for exchange settlement marks.

The new standalone `lab.crash_funding` module queries
`GET /fapi/v1/fundingRate` with an **inclusive lower bound and an upper
bound of exactly one millisecond before the existing crash holdout**.
It validates symbol, strictly increasing settlement timestamps,
positive settlement mark prices, finite rates, and continuity with a
conservative eight-hour-plus-30-second gap tolerance for this BTCUSDT research protocol.
Funding intervals may change; no one is entitled to assume all symbols
always settle exactly every eight hours. Gaps fail closed. The source
plan pins each canonical JSON response page and the normalized event
sequence by SHA-256; funding history is not a market-data repair.

The initial survey is read-only. `--plan-out` explicitly saves the source
manifest; an explicit `--apply --plan` re-fetches and verifies all pages
before **append-only** publication of a separate event cache. `--audit
--plan` independently verifies cached event/manifest digests with no
network access. Nothing touches migrated OI files, candle archives or
protected holdout data.

The pure `settlement_return` accounting function values funding at
each validated settlement's *exchange-reported mark price*, relative
to the entry reference notional. An event falling exactly at an
entry/exit timestamp is ambiguous at minute resolution and fails
closed rather than assigning a fabricated payment or receipt.
**This function is not yet wired into crash strategy P&L**: historical
event coverage must first be independently demonstrated and accepted.
The current strategy trial still explicitly declares funding
`not_modelled`. No new strategy results or holdout tests are authorized.

First run the offline tests:

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_funding.py tests/test_crash_execution.py \
      tests/test_crash.py -v

Then perform only a bounded public-source survey in Termux:

    PYTHONPATH=src:. python -m lab.crash_funding \
      --config ~/trading-termux-tests/lab.toml \
      --symbol BTCUSDT --start 2024-01-01 \
      --plan-out ~/trading-termux-tests/btc-funding-preholdout-plan.json

The source plan must be inspected before any apply. This new
functionality is **not yet externally validated** and funding is not
yet reflected in strategy returns.

## Crash funding settlement ownership bounds (pending Termux validation)

The operator's first full 2024-01-01 to 2026-05-07 funded BTC
breakdown attempt stopped with
`ambiguous funding settlement inside unknown exit-minute execution
interval`. This was an expected fail-closed response to minute-OHLC
rather than timestamped order fills, not evidence of corrupt funding
or negative/positive strategy profitability. No funded result was
published from that attempt; the durable failed trial remains counted.

The replacement model
`audited-fixed-base-mark-minute-fill-funding-bounds-v2` keeps **all**
strategy and baseline trades, including those whose minute-range
executions overlap a recorded funding settlement. It computes
deterministic funding P&L bounds using the exact audited Binance event
timestamp, rate and settlement mark:

- A settlement strictly after the 30-second entry uncertainty and
  strictly before the earliest possible exit (end of bar minus one
  minute and 30-second uncertainty) is **certainly owned** and its
  actual funding cashflow is included in both bounds.
- In either uncertainty window, an economically positive payment is
  **excluded from the pessimistic lower bound** but included in the
  optimistic upper bound; an economically negative payment is
  included as a **cost in the pessimistic lower bound** but excluded
  from the upper bound.
- All remaining settlements are excluded as outside the position.
  No trading-rule thresholds, signals, exits, size or trade count
  are altered based on funding profitability.
- The principal `net` and net-based research summary use the lower
  funding bound. The `net_upper`, `funding_return_upper`,
  funding-bound width, ambiguous settlement count, and a distinct
  upper-scenario summary are retained separately.
- Identical uncertain funding cashflows and their bounds are
  preserved when rescoring 20/30/50-bp taker execution scenarios.

These are **conservative scenario limits**, not proof that the
pessimistic bound was an actual realized payment. The uncertainty
windows are model assumptions, not measured exchange fill timestamps.
They do not address partial fills, margin, liquidation, dynamic
mark-to-market collateral, intrabar price path or separate
settlement-day P&L. The research is not yet eligible for holdout
evaluation or supervised execution.

Termux acceptance and an actual funded exploratory study result
are pending. The original source-audited funding, OI and candle
caches are not rewritten.

## Audited funding integration in exploratory BTC short research (pending Termux tests)

The operator completed pre-holdout funding evidence acceptance:
`lab.crash_funding --apply` returned `status=committed`,
`events=2574`; the independent read-only audit returned
`ok=true` and `issues=[]`. No protected-holdout records were read.

The optional `lab crash study --mode breakdown --symbols BTCUSDT
--funding-plan /path/to/btc-funding-preholdout-plan.json` now:

- Reserves a durable trial **before** loading audited settlement events,
  one-minute candles, or five-minute open interest.
- Independently audits the pinned local funding cache on every funded
  study; fails the trial on a missing, altered or incoherent source.
- Fingerprints every actually used millisecond-level funding timestamp,
  rate and exchange mark price in the same trial as the candle and
  OI streams. The source-plan event SHA-256 and funding model version
  are recorded in trial parameters and results.
- Applies positive rates as receipts for a short and negative rates
  as payments, using fixed-base position quantity and the *exchange
  mark price at each settlement*. Original entry/exit decisions,
  volatility sizing and price/fee assumptions are unchanged.
- Adjusts both the breakdown strategy and its daily-entry baseline,
  and carries identical audited funding cashflows through the fixed
  20/30/50-bps execution-cost stress scenarios.
- With the v2 funding-bound correction documented above, does not
  drop trades at ambiguous exit-minute settlements. It instead books
  the pessimistic settlement bound in the primary research result
  and retains a separate optimistic bound with uncertainty counts.
  Neither bound is claimed to be the exact realized cashflow.

The ordinary no-`--funding-plan` crash study remains explicitly
`funding_status=not_modelled` and cannot be called funding-adjusted.
Only single-symbol BTCUSDT has an accepted source plan at this point.

**Limitations:** Futures account balance, leverage, margin, liquidation,
actual execution timestamp, funding attribution within exit minutes,
partial fills, mark-vs-trade-price collateral variation, cost
calibration, and statistical strategy qualification remain unresolved.
In this provisional study, total realized trade P&L (including funding)
is grouped on the exit day for summary statistics; funding cashflow is
not yet marked as separate settlement-day daily P&L. It therefore
must not be treated as a qualified daily Sharpe or institutional
economic ledger.

After external tests pass, an exploratory pre-holdout funded run can
be invoked with:

    PYTHONPATH=src:. python -m lab --config \
      ~/trading-termux-tests/lab.toml crash study \
      --mode breakdown --symbols BTCUSDT \
      --funding-plan ~/trading-termux-tests/btc-funding-preholdout-plan.json

**The original funding integration passed 92 Termux regression tests.**
The subsequent real-data run encountered an ambiguous settlement and
did not publish a funded performance result. The v2 bounded-settlement
correction above is pending its own external regression test. This
remains research-only, logs durable attempts, and is not a
paper-trading, live-execution or holdout authorization.

## Research attempt provenance

The ported `lab crash study` CLI now reserves each research attempt durably
**before** reading klines or open-interest history. Interrupted/failed runs
remain counted in the trial ledger and include source/runtime provenance.
Its `oi_measure` fingerprint distinguishes contract-based studies from old
notional-based results. Input-content hashes and a comparable multi-trial
inference method are not yet qualified, so the CLI does **not** produce a
deflated Sharpe qualification. Holdout study/release remain blocked.

## Termux acceptance

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash.py tests/test_crash_oi.py -v

    PYTHONPATH=src:. python -m pytest \
      tests/test_market_journal.py \
      tests/test_market_capture.py \
      tests/test_market_archive.py \
      tests/test_market_quality.py -v

The crash modules were selectively ported from `main`. The branch is
still divergent from `main`; this is **not** a completed branch merge.
Reconcile the broader research provenance / holdout lifecycle before
claiming the entire historical crash pipeline is institutionally
qualified. In particular, do not use the old holdout entry point until
its pre-access reservation semantics have been reviewed against the
maintenance-branch trial system.

Earlier BTC short-side experiments yielded negative performance and
are not converted into a passing trading strategy by correcting the
measurement. All candidate strategies remain unqualified.
