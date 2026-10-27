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
