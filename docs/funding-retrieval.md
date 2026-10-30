# Funding retrieval and event time (B1b)

Funding has variable settlement intervals. A missing event row cannot establish that an hour paid zero:
the period may simply never have been downloaded. This branch records successful API query windows and
requires that evidence for carry evaluations, without assuming an eight-hour historical schedule.
This is **retrieval coverage**, not independent verification of every settlement the venue should have paid.
Historical schedule coverage remains unverified, and B1 and statistical qualification remain incomplete.

## Two regressions

Before this change, a cache containing rates but no query evidence could produce a carry result; absence
became zero funding. A separate regression showed an 08:30 settlement entering the 08:00 signal because
events were grouped by `floor('h')`. Both tests failed before the fixes.

The hourly interval ending at T is now `(T-1 hour, T]`. Events use `ceil('h')`: an exact 08:00 event remains
at 08:00, while an 08:00:00.001 or 08:30 event belongs to 09:00. Thus a decision cannot inspect that delayed
event at 08:00. Existing ordering is preserved for exact-hour funding: it settles before that hour's decision,
so a new position cannot earn the event which triggered it. Decisions/positions remain on the hourly grid;
this does not simulate subhour execution or funding-publication latency.

## Rates and query evidence in one snapshot

The existing `<venue>-perp-funding-<symbol>.parquet` contains the rates and schema metadata under
`lab.funding-retrieval-v1`. That strict JSON object identifies version, venue and symbol, and records the
canonical union of inclusive `[start_ms, end_ms]` query intervals. Only fully successful, validated,
paginated requests contribute intervals. A successful empty response records a queried period without
inventing an event. It is distinct from an unqueried period; it does not prove market/listing availability.

Missing prefix, internal and tail query intervals are downloaded with bounded requests. Incremental fetching
advances past the last **queried** millisecond, including zero-event hours, rather than only the last returned
settlement. A cache whose requested window is already covered needs no funding request. Previously observed
events inside a re-queried legacy window must still appear in the API response; a contradiction stops the
update and retains the original cache. No command silently deletes such evidence to manufacture coverage.

The entire Parquet snapshot is written to a temporary file, fsynced, atomically renamed and its directory
fsynced under the existing per-series lock. Rates and query evidence are never published separately. An
interruption during pagination or before replacement leaves the prior snapshot. A failure after replacement
can report an error while leaving a coherent new snapshot; it cannot leave new query evidence with old rates.
Readers hold one file descriptor across schema/data reads, so a concurrent rename cannot combine two
snapshots. Concurrent writers merge under the same lock. Checkpointing remains per completed series update,
not per page or query interval; interrupted downloads can repeat work.

API and metadata checks reject unordered/duplicate/out-of-window records, nonfinite rates, non-millisecond
event timestamps, unknown schema versions, identity mismatches, malformed/noncanonical intervals, nonfinite
JSON and duplicate keys. No keys, account data, paid feed or exchange-order path is introduced.

## Study requirements and provenance

An hour may use zero for an absent settlement only if its entire millisecond event interval was queried.
Otherwise funding is unavailable. Research evaluation requires coverage for its observed price window;
holdout evaluation also requires the preceding signal-lookback window. Missing coverage fails the evaluation,
including when the strategy would have stayed flat. The lower-level backtest also refuses to value a held
position through unknown funding. Cross-venue coverage is the intersection of both venues' query windows;
one unqueried venue cannot be treated as a zero-paying leg.

Research drops held-out rates before validation, aggregation or cross-venue difference arithmetic. Holdout
data are bounded by the already-reserved upper cutoff. Only the relevant hourly coverage mask is hashed with
the actual bounded study inputs; later query extensions and held-out rate changes cannot change a research
input digest. Missing query evidence and failed evaluations remain in the existing durable trial history.

Every carry configuration now includes `funding_policy=queried-event-time-v1`, including automatic comparisons.
The corrected timing is a different identified evaluation policy; earlier attempts are retained. It does not
reset trial counts, certify a research family, change a profitability/statistical threshold, or qualify the
frozen daily-v1 diagnostic, whose independent validation still failed.

## Existing caches

Legacy funding files have no query evidence. They remain readable for inspection, but cannot establish
coverage for a study. Re-run the normal fetch with the oldest required start, for example:

```sh
lab carry fetch --symbols BTCUSDT ETHUSDT --since 2023-01-01
```

The fetcher re-queries uncovered intervals and atomically stores source evidence with the existing rates.
There is no flag to declare an old file complete without querying it. Keep prior attempts and data backups:
repaired inputs or the changed policy may make earlier results incomparable. Test fixtures explicitly declare
their known synthetic source ranges; that fixture helper is not a production migration mechanism.

## Limits and remaining work

The API is trusted to return its available records under its documented pagination contract. A successful
request does not prove the venue retained all historical events, reported them without omission, or used a
particular historical funding schedule. Prefixes before listing, delistings, venue revisions and independent
historical settlement-schedule verification remain open. The CLI continues to state that distinction.
These local metadata are mutable, not a signed or tamper-proof audit: an external writer that forges or
preserves evidence while removing events can invalidate its meaning. Use the normal fetch path and retain
source backups; a third-party rewrite that drops the metadata becomes unverified rather than inheriting proof.

No full-universe historical run or candidate evaluation was performed in B1b. B2/B3 empirical studies,
B4 collateral/liquidation/rebalancing/survivorship economics, A8b inference qualification and all holdout
gates remain pending.

Primary contracts: [Bybit funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)
and the [Binance official funding-history connector documentation](https://binance.github.io/binance-connector-js/classes/_binance_derivatives-trading-usds-futures.DerivativesTradingUsdsFuturesRestAPI.MarketDataApi.html).
