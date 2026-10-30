# Historical perpetual mark candles (B4c)

Liquidation modelling needs mark prices, while the carry ledger and B4b diagnostic explicitly use trade-close
proxies. B4c adds separate public mark-price history ingestion as a prerequisite. It does not change the
strategy, funding calculation, collateral diagnostic or any qualification threshold.

```sh
lab carry fetch-marks --symbols BTCUSDT ETHUSDT --venues binance bybit \
  --since 2023-01-01 --until 2026-10-29T00:00:00Z
```

`--until` defaults to one frozen UTC wall-clock cutoff for the whole command. Future cutoffs are rejected
before downloading. Dates without timezone mean UTC; offsets are converted to UTC. CLI symbols are
uppercased and must name alphanumeric USDT pairs. This public-data path has no keys or order capability,
does not discover a listing universe and does not run a strategy.

## Sources and timing

- Binance USD-M: [`GET /fapi/v1/markPriceKlines`](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price-Kline-Candlestick-Data), interval `1h`, forward pages of at most 1,000 rows.
- Bybit linear: [`GET /v5/market/mark-price-kline`](https://bybit-exchange.github.io/docs/v5/market/mark-kline), interval `60`, backward pages of at most 1,000 rows. Response symbol/category must match the request.

Native bounds name candle OPENS; the cache names ENDS. An opening at 07:00 becomes the row ending at 08:00.
Only whole candles opening at or after `ceil(since)` and ending no later than `floor(until)` are requested.
With `since=07:15`, the first requested candle opens at 08:00 and ends at 09:00. Binance's close timestamp
must equal opening time + one hour - one millisecond. A current open candle is not accepted.

A row describes the preceding hour, not an exact observation at its end timestamp. Its extrema are known
only after that hour. A future consumer must not expose them to an earlier signal or attribute them to a
position opening after the interval. Hourly closes are not exact funding-event marks; OHLC does not reveal
the sequence or timing of intrahour extrema across venues.

## Cache integrity and repair

`data/carry/<venue>-perp-mark-ohlc-<symbol>.parquet` holds `ts, open, high, low, close` together. Identity is
embedded under metadata key `lab.mark-ohlc-v1`: model `hourly-perpetual-mark-ohlc-v1`, venue, symbol, interval
3,600,000 ms, hour-end convention and mark-price kind. This is separate from trade-price files. Missing marks
stay missing; traded spot/perpetual prices never substitute for them.

Readers use one file descriptor for data and metadata, so atomic replacement cannot mix snapshots. Physical
Arrow timestamps must already have UTC timezone: pandas metadata cannot silently localize naive physical
timestamps. Exact columns, ordered unique aligned UTC times, numeric finite positive values and consistent
low/open/close/high bounds are required. Missing/conflicting identity, duplicate JSON keys and invalid
observations stop updates instead of relabelling the cache.

Native pages are checked for exact row shapes, bounds, direction, uniqueness, alignment and price consistency
before advancing. Repeated full pages fail instead of looping. Existing retry/backoff handles temporary rate
limits and network failures; permanent errors and malformed responses remain failures.

Updates group every missing requested closed hour into contiguous ranges and download only those ranges.
Leading gaps are retried too: an empty API response is not listing-availability proof. No placeholders or
forward filling are added. A complete cache needs no request; extensions include the candle opening exactly
at the previous last cached end. Known rows are retained without automatic source-revision auditing.

Writers serialize per file and reload its current snapshot under the lock. All missing ranges finish and the
merged frame validates before publication. OHLC and identity are written to one temporary Parquet file,
fsynced, atomically renamed and followed by directory fsync. Failures before rename retain the prior file
and clean the temporary file. A failure after rename cannot promise rollback; the command returns incomplete.
Progress is per completed venue/symbol, not per page/range. An interrupted large symbol may repeat downloads
that had not reached publication; completed files remain usable.

## Coverage and holdout separation

`<venue>-perp-mark-coverage-<symbol>.json` records the requested window, observed/missing counts, contiguous
gaps and unverified leading hours. States are `complete-requested-window`, `gappy`, `unavailable` or
`empty-window`. Only a nonempty full grid has `complete_requested_window=true`. Unknown leading prefixes are
included in missing counts, rather than removed from the completeness calculation.

Errors write `fetch-error`, the requested window and `complete_requested_window=false`, replacing stale
successful reports if storage permits. Report-write failures are printed and return nonzero. Other venues/
symbols still run; completed data remain. Empty answers, gaps, invalid caches and access errors return nonzero.
Listing availability, funding-event marks and liquidation qualification are always explicitly unverified.
These reports are derived snapshots, not independent proof that the API retains every historical observation.

The study window is anchored to trade closes, turnover and funding. Auxiliary marks therefore cannot choose its
first holdout boundary, extend a holdout cutoff or determine a release boundary. Two regressions failed before
restricting discovery: newer marks moved the inferred end of strategy data, and mark-only history initialized
a holdout partition. An explicit source allowlist fixes both. Fetching creates no strategy trial. The subsequent
[B4d optional mark diagnostic](carry-mark-collateral.md) truncates under reserved evaluation windows and hashes
bounded OHLC in input provenance; the default remains the separate trade-close diagnostic.

## Verification and remaining work

Tests use deterministic fixtures shaped from official API contracts: both page directions, closed-hour timing,
resume, prefix/internal/tail repair, empty answers, malformed/repeated pages, physical/schema identity,
prepublication failures, snapshot readers, competing writers, retries, CLI reports/errors, future cutoffs and
holdout separation. Full suite: 552 passed on Python 3.13.16; lint/format passed.

The 2026-10-30 live BTCUSDT smoke request for 2026-10-29 00:00–02:00 UTC could not establish live access:
Binance returned HTTP 451 and Bybit returned a non-JSON response. No live candles or empirical strategy result
were accepted. Full-universe/history coverage has not been downloaded or verified here.

Bounded mark consumption is now implemented in B4d. Remaining B4 work includes historical margin tiers and initial
margin admission, close-fee reserves and liquidation execution/losses, exact-event funding valuation,
transfers/financing, rebalancing and survivorship. A full OHLC grid alone cannot qualify B4. A8b inference
remains unqualified after its archived failed independent validation. Phase 1 continues; Phase 2 has not started.
