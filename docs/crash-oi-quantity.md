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
