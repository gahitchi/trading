# Binance USD-M Live Upgrade — Part 1 (public ingestion, no trading)

This branch delivers a **real external exchange market-data acquisition** path,
not simulated ticks. The six-part offline laboratory remains preserved for
deterministic regression testing but is **not** the source of any live quote.

## Supported environment

- Mainnet REST: \`https://fapi.binance.com\`
- Mainnet WebSocket **public**: \`wss://fstream.binance.com/public/stream?streams=...\`
- Mainnet WebSocket **market**: \`wss://fstream.binance.com/market/stream?streams=...\`
- Testnet REST: \`https://testnet.binancefuture.com\`
- Testnet WebSocket: \`wss://stream.binancefuture.com/stream?streams=...\`
- Maximum 9 configured USD-M USDT-settled perpetual symbols initially,
  matching the existing operator's 9-bit symbol mask.

**October 2026 contract change:** Binance has retired legacy unrouted
mainnet market streams. Using the same \`/stream?streams=\` connection for
depth and all other stream classes misses mark price and trades. This
implementation uses two independent sockets. The documented depth feeds
have 100ms and 500ms modes; \`@depth@0ms\` is not treated as a guaranteed,
universally supported contract. We select \`@depth@100ms\` rather than
misrepresent its latency. A future qualified exchange-specific feed can
replace this source without changing the book/state interface.

## Exact contract

1. \`GET /fapi/v1/exchangeInfo\` builds immutable \`InstrumentSpecTable\`
   from *PRICE_FILTER*, *LOT_SIZE*, *MARKET_LOT_SIZE*, and *MIN_NOTIONAL*
   filter records. The exchange's presentation \`pricePrecision\` and
   \`quantityPrecision\` are **not** substitutes for its actual increments.
   Symbols must be TRADING USDT-margined perpetuals. Missing, disabled, or
   unrepresentable filters are rejected; no invented defaults.
2. Core types contain integer \`InstrumentSpec\` and cacheline-aligned
   \`ExecutionReportEvent\` (the latter is a wire contract for **Part 2**;
   there is no private execution stream or fills in this part). Tick price
   rounding accepts an explicit Up/Down mode and quantity uses
   deterministic non-expanding truncation. Tick/lot/max/min/notional
   validation remains a mandatory exit boundary for Part 2.
3. Two real \`tokio-tungstenite\` sessions consume the exchange's public
   diff-depth and regular market streams: \`aggTrade\`, \`forceOrder\`,
   \`markPrice@1s\`. A common Core 0 dispatcher is the only producer of
   the \`rtrb\` SPSC ring; Core 1 owns its consumer and \`DepthSynchronizer\`.
   Per-depth-frame metadata preserves **U, u, pu and complete bid/ask batch**.
   It never reconstructs batches from isolated 64-byte MarketEvents.
4. Core 1 begins buffering live diffs before requesting REST snapshots.
   Requests are bounded and asynchronous (\`GET /fapi/v1/depth?limit=1000\`).
   A snapshot includes the request's generation and ID, so a late response
   from an earlier reconnect cannot revive a disconnected book. The
   existing Binance USD-M synchronizer validates snapshot bridge and
   subsequent exact \`pu == prior u\` continuity.
5. WebSocket disconnects, malformed frames and invalid sequence continuity
   invalidate the book; bounded queues return Backpressure instead of
   quietly dropping market events. Network workers use exponential-backoff
   reconnection and process WebSocket ping/pong. Streaming test harnesses
   parse static **exchange-shaped fixtures** only in tests, not in the
   production network loop.

## Run (read-only and no API keys)

\`\`\`bash
cd ~/trading-rust
git fetch origin
git switch chatgpt/live-upgrade-market-data-20261027
cd rust

cargo fmt --all -- --check
cargo test --workspace --locked --all-targets
cargo clippy --workspace --locked --all-targets -- -D warnings
cargo build --workspace --locked --release

cargo run --locked --release -p trading-market-data --bin market-watch -- \
  --environment testnet --symbols BTCUSDT,ETHUSDT,SOLUSDT
\`\`\`

Choose \`--environment mainnet\` to **read public real-market data** without
private credentials; no order execution code is connected. Ctrl+C stops
the observer. Testnet availability and endpoints can differ; verify with
actual network smoke tests on the target Arch host.

## Remaining mandatory gates — not yet production-qualified

- Exact live Binance public payload/REST shape verified from the current
  documentation but **not yet validated against user's live Arch network**.
- Real market-data **timestamp accuracy**: exchange Unix timestamps vs
  receive wall-clock timestamps cannot by themselves prove 150ms of one-way
  latency. NTP/PTP health, uncertainty bounds, and per-feed staleness
  monitoring must be integrated before using the Part 5 latency gate.
- Event burst and memory pressure telemetry, reconnect fuzzing, 24h rotation
  tests, rate-limit/429 handling, websocket backpressure incident recovery,
  per-symbol status changes, and measurements on pinned cores.
- USD/USDT collateral accounting and no-fake-equity guarantees require
  independently reconciled broker balances, borrowed funds, liquidation
  and cross-margin liabilities, not the Part 3 synthetic wallet.
- Execution gateway and user data stream belong to **Live Upgrade Part 2**.
  No signed endpoints, no keys, no market orders, no simulated fills, and no
  promotion to trade-eligible status in this part.
- Venue-aware real fill/wallet **reconciliation and rollout controls** belong
  to Part 3 of this live upgrade. Until fully qualified, do not enable
  automatic real-money orders. The legacy collaboration policy "paper
  accounts only" remains active until explicitly amended and reviewed.

## Native Rust roadmap

1. **This pass:** Live public REST/WS, instrument metadata, depth
   synchronization and execution-report layout.
2. **Part 2:** Signed WebSocket order API (Ed25519 session/HMAC), API key
   isolation, real listenKey/user data stream, order/report parsing, durable
   idempotency and explicit mainnet-vs-testnet operational authorization.
3. **Part 3:** Broker-verified startup bootstrap, real account-position
   reconciliation, exchange-confirmed fills/commissions/funding/insurance,
   live risk gate and kill switch, reserved order state, end-to-end
   acceptance, rollout gates and performance testing. No fake fills.

## Arch 2026-10-28 public-feed incident and snapshot protection

The 39-second BTCUSDT/ETHUSDT/SOLUSDT Testnet run established both WebSocket
connections and received live depth, trade and mark price events. However,
all three books repeatedly cycled through `Activated` and `NeedsSnapshot`.
One 5-second interval showed 124 cumulative REST depth snapshots after
approximately 30 seconds. **Do not qualify this feed for order routing** on
the strength of a transient `Activated`.

The follow-up changes add:
- One global REST depth snapshot start per second, with no more than two
  concurrent requests. At a weight of 20 per 1000-level request, this caps
  sustained depth-snapshot demand to around 1200 weight/minute, before other
  clients or API calls on the same IP.
- Per-symbol coalescing so obsolete pending snapshot generations/IDs never
  trigger a burst of redundant HTTP requests.
- Error-safe, bounded SPSC handoff remains mandatory; no fabricated
  continuity or forced `Live` state.
- Diagnostic `resync` records identifying the affected symbol, preceding
  state, prior local `u`, incoming `U/u/pu`, and failed snapshot ID.
  The first 24 incidents are logged and later incidents sampled to avoid
  unlimited console output.
- Regression checks that reject mismatched `pu`, and that verify snapshot
  request coalescing and round-robin scheduling.

**Root-cause uncertainty:** The first diagnostic log does not include
per-event sequence IDs and therefore cannot distinguish a genuine Binance
Testnet `pu` gap, an invalid post-snapshot bridge, or an order-book
state-machine error. Never disable continuity validation to hide this
problem. Use the new resync details to localize the cause on Arch before
claiming production-quality market-data acceptance.

## 2026-10-28 Arch root-cause correction: Futures `pu` chain vs Spot `U`

A subsequent Arch Testnet replay showed all public WebSocket streams connected,
but the books repeatedly lost `Live` state despite **incoming `pu` exactly
equaling the previous local `u`**. Example:

```
prior local u = 465705655761
incoming U   = 465705656237
incoming u   = 465705656825
incoming pu  = 465705655761
```

The previous synchronizer incorrectly required both `pu == previous u`
and `U <= previous u + 1` for *each live Futures batch*. The extra
`U` restriction is the Spot depth-stream rule; Binance USD-M Futures
instead specifies `pu == prior u` for subsequent live batches, and
legitimately has jumps in `U` between published depth batches.

The code now requires **strict equality of `pu` and previous `u`** for
subsequent Futures updates and rejects mismatches. The **initial snapshot
bridge** continues to require `U <= lastUpdateId <= u`. Stale/replayed
events continue to be ignored; crossed order books and malformed batches
fail closed. Regression tests include the real Arch sequence values and
a deliberately false `pu`.

The one-per-second global snapshot limiter remains as defense in depth.
Before qualifying live trading, repeat the Arch read-only market-watch
test and establish stable `live_books=3` with near-zero post-initialization
resynchronizations. Existing snapshots and SPSC backpressure must still
be verified under sustained load.

Source: Binance USD-M Futures documentation, "How to manage a local order
book correctly", last modified 2026-10-28.
