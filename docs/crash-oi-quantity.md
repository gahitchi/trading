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
    from datetime import date
    from pathlib import Path
    from lab import crash_data as cd

    root = Path.home() / "trading-termux-tests/data/crash"
    pending = cd.migrate_oi_quantity(
        root, "BTCUSDT", before=date(2026, 5, 8)
    )
    print("Legacy pre-boundary OI files:", len(pending))
    print("First 5:", pending[:5])
    PY

The default is **dry run**: no archive download or Parquet rewrite.
After the archive source/holdout path is verified, the operator may
explicitly call `migrate_oi_quantity(..., apply=True)`. That operation
fetches each original daily Binance metrics ZIP, compares every timestamp
and notional OI value against the local cache, then atomically adds
`oi_qty` only if the input matches. On any mismatch the affected period
is preserved; migration can be resumed on the remaining legacy days.
It may require many archive downloads and rate-limit backoff.

Research-only crash studies use pre-boundary OI periods and never open
post-boundary OI cache files. A *future* authorized holdout evaluation
must first verify the held-out OI data's quantity representation, but
must never be used to fit thresholds.

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
