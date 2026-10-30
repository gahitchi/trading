# Carry history integrity (B1a)

Carry studies need reliable history before they can test an edge. This branch fixes a reproducible cache
resume error and adds hourly gap repair and explicit coverage. It does not declare B1 complete, run the
registered altcoin strategy, verify all funding settlements, or qualify statistical inference.

## Timestamp contract and reproduction

The native candle APIs identify bars by **opening** time. The cache identifies each hourly close and its
turnover by the **end** of that hour: `cached_ts = candle_open + 1 hour`. A cached close at 05:00 therefore
requires the next request to include the candle opening at 05:00. The old `last_cached_ts + 1 millisecond`
cursor excluded it. An initial five-hour fetch followed by an update to ten hours returned only nine closes.
Six regressions failed before the fix: resume and existing-gap repair on Binance futures, Binance spot and
Bybit perpetuals. The fixtures apply API bounds to actual opening timestamps rather than generating rows
from the requested cursor.

Funding is a separate event stream: its timestamp is the settlement time. B1b now records successful query
windows and advances past the last queried millisecond, including empty periods; see [funding retrieval](funding-retrieval.md).
Binance funding requests also send their frozen upper bound.
The snapshot cutoff for a fetch is fixed once at CLI startup. Candle requests exclude opens whose hour has
not finished by that cutoff. No incomplete candle is stored as a final close.

Primary API references: [Binance futures connector's candle contract](https://github.com/binance/binance-futures-connector-python/blob/main/binance/um_futures/market.py),
[Binance spot API](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md), and
[Bybit candle API](https://bybit-exchange.github.io/docs/v5/market/kline).

## Repair policy

Every close update checks both the close and turnover timestamp sets. Within the requested window and from
the earliest observed candle, their intersection identifies hours already present in both columns. Missing
hours are grouped into contiguous intervals and requested with bounded native opening-time ranges. Intact
intervals need no download. Both columns are merged from the same returned klines, without duplicate cache
timestamps. If turnover alone was missing, the returned close for that interval may also refresh an existing
close; venue revisions can consequently change input hashes.

The tail through the latest closed hour uses this same policy. Re-running an up-to-date cache makes no candle
request. If a companion file is absent, it is backfilled from the requested start. If saving the close file
succeeds but saving turnover fails, the next update detects and repairs the remaining difference. Each file
is still replaced through the existing temporary-file/rename path; the two files are not an atomic database
transaction. Progress is saved per completed series update, not per API page or repair range. A process lost
mid-download may repeat that download, while earlier completed series remain cached.

API pages must have unique ordered timestamps, lie inside their current request bounds and use whole hourly
opens for candles. Repeated full pages fail on the second request rather than looping forever. Funding must
be finite, closes finite and positive, and turnover finite and nonnegative. Invalid pages cannot replace cached
values. Existing caches with invalid values, unordered/duplicate/missing timestamps or unaligned hourly rows
are rejected without silently rewriting them. Restore invalid evidence from a verified source; do not trim
research logs to improve a result.

Unknown history before the earliest observed candle is labelled separately. It might predate listing, but
the fetcher does not assert that explanation or repeatedly re-download the whole prefix. Later requests with
an earlier `--since` do not prove that this prefix is unavailable. Listing-aware historical discovery and
backfill remain open. Internal/trailing holes are retried on the next fetch; if the API still omits them,
they remain actual missing rows. No zero, carry-forward close or turnover placeholder is written.

## Coverage and command result

After each successful close update, `lab carry fetch` saves a snapshot next to the Parquet files:
`<venue>-<market>-coverage-<symbol>.json`. It contains the requested start, frozen latest-closed-hour cutoff,
first observed hour, leading hours with unverified availability, per-column row/missing counts, and every
missing interval with inclusive UTC end-time bounds. Its states are:

- `continuous-observed-window`: both columns cover the observed window through the cutoff.
- `gappy`: an observed window exists but close or turnover hours are absent.
- `unavailable`: no candle is observed inside the requested window.

These are snapshots for their saved cutoff, not a live health guarantee. If an update fails, an older coverage
file may remain; the CLI reports the failure and the old file's cutoff still identifies its age. A missing
prefix remains unverified even when the observed window is continuous. Empty history never establishes
coverage merely because it contains no measured gaps.

The command exits 1 when a requested series errors, has no cached rows, or hourly coverage remains gappy or
unavailable; other successful series stay saved. It exits 0 only when these fetch checks finish without a
blocker. Neither exit code is a research/strategy gate. Every coverage report explicitly records
`funding_schedule_verified=false`, and the CLI repeats that limitation.

Funding intervals vary by contract and time. Candle continuity and finite cached funding events cannot prove
that every expected settlement was downloaded. B1b records/repairs unqueried funding windows and blocks studies
without that retrieval evidence; it does not verify historical schedules or detect every venue-side omission.
Historical settlement schedules, listing/delisting availability, a full-universe data run and the B2/B3 empirical studies remain open.
The existing strategy's price-gap, holdout, trial-provenance and failed-inference safeguards still apply.
Repairing inputs can invalidate comparison with earlier identified inputs; retain all prior attempts instead
of erasing their history. No strategy setting, financial gate threshold, statistical estimator or risk limit
changes in B1a.
