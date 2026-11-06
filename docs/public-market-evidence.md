# Public USD-M mark and historical funding-rate evidence

**Status:** implemented as a portable read-only research collector and
synthetic CI regression tests; real venue connectivity and cross-source
valuation are **not yet externally accepted**.

Module: `lab.public_market_evidence`; CLI: `lab public-market-sample`.
This does not read private accounts, take API keys, submit orders,
reconcile real funding payments, value margin or publish P&L.

## Why this source is separate

The existing `lab.nautilus_snapshot` records the NautilusTrader
**local simulator's** account position, outstanding orders and cash.
Its own event stream is not an independent check of external prices.
This new collector reads **public USD-M futures REST** from Binance,
using only two fixed GET endpoints, independent of the Nautilus
sandbox's local book state:

- `GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT`
  yields a timestamped `markPrice`, `indexPrice`,
  `lastFundingRate` (latest *rate*, not a payment) and
  `nextFundingTime`.
- `GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1`
  returns the last historical funding-rate event including
  `fundingTime`, `fundingRate` and its historical `markPrice`.

These are **public market observations**. They are neither the actual
sandbox fill price nor account-specific funding cash transfers, and
a reported `nextFundingTime` is not proof that a payment happened.
See Binance's [USD-M REST market data reference](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price).
Future funding schedule/intervals and venue metadata must be
revalidated before settlement modelling.

The collector preserves original UTF-8 JSON payloads as strings,
SHA-256 digests, venue timestamps in milliseconds and a local epoch
nanosecond before/after interval **for each** of the two network
calls. The observed venue mark must lie within a conservative
30-second past / 2-second future window relative to the collector
clock; requests longer than 10 seconds, invalid symbols, incomplete
JSON, NaN, nonpositive prices, stale historical rates older than
seven days, and a missing/expired next funding time fail closed.
The collector does not silently infer gaps, clocks or market marks.

## Collection command

From the repo's configured `lab` entrypoint, for a paper session:

```bash
lab public-market-sample BTCUSDT --session paper-20261106-a
```

By default the evidence archive is:

`results/fast/public-mark-paper-20261106-a-BTCUSDT.jsonl`

Use `--out /path/to/file.jsonl` to select another archive.
The journal contains a source/session/instrument header, ordered
SHA-256 predecessor-linked sample rows, and an atomically fsynced
`.head.json` checkpoint. Source JSON is re-parsed and verified on
every replay. Duplicate, overlapping or out-of-order observation
windows, changed fields, interrupted writes and missing checkpoints
make the archive unusable until an audited recovery.

**No API keys** are needed. The CLI performs the remote requests
before creating any archive so a refused HTTP request does not create
an apparently complete journal. Requests are bounded to 64 KiB each
with an eight-second network timeout. Connection failures, API
restrictions or local clock problems must be reported as errors,
not filled with synthetic prices.

There is no periodic sampling scheduler yet; the command captures
**one pair of responses** per invocation. It is intentionally
separate from the simulated execution callback's synchronous
hot path.

## Evidence and risk boundaries

- HTTPS public response collection and SHA-256 protect transport
  and detect local file changes. They do **not** authenticate the
  resulting archive against an attacker who can rewrite all local
  files or prove the venue's full historical book state.
- `premiumIndex.lastFundingRate` is not a realized account credit,
  `fundingRate` history is not a wallet statement, and neither
  can be used to directly create a `ProfitJournal` trade or
  funding adjustment.
- The public mark sample does not prove that a particular trade,
  quote, book update, liquidation mark or funding settlement was
  observed by the Nautilus engine.
- Symbol scope is USD-M **USDT** perpetuals only. Coin-M,
  inverse contract multipliers, FX, USD-M non-USDT margin assets,
  and alternate venues need their own validated contracts.
- Future qualified P&L reconciliation requires separate fill/fee
  provenance, contract specification and multiplier, synchronized
  market/book mark streams, and executed funding cash records.
  No trading authorization is granted by this module.

## Verification

Fully portable tests (no native NautilusTrader dependency):

```bash
PYTHONPATH=src:. python -m pytest tests/test_public_market_evidence.py -q
```

The regression suite injects fixed REST responses and clocks,
exercises exact decimal strings, symbol and timestamp validation,
long/missing responses, replay hashes, checkpoint failures, duplicate
events, and the CLI's fail-before-write behavior.

**Still outstanding:** supervised external REST capture on Arch or
a working networked paper host, multi-source timestamp alignment,
actual account funding settlements, margin/equity reconciliation,
recovery drills and investment return validation.
