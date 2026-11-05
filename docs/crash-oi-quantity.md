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
