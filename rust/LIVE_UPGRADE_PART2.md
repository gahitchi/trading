# Live Upgrade Part 2 — Binance Futures authenticated execution transport

**Branch:** \`chatgpt/live-upgrade-market-data-20261027\`

The new \`crates/execution\` is a Rust-only Binance USD-M Futures
authenticated transport layer. It is *not* a production trading application:
it cannot independently establish true broker/account reconciliation,
persistent exposure reservations, hedge-mode parity, fee asset conversion or
a qualified live kill/flatten service. These are hard Part 3 requirements.

## Implemented

- Real Binance WebSocket API client with Mainnet and Testnet endpoints.
  HMAC-SHA256 per-request signing using canonical lexicographically sorted
  parameters; Ed25519 PKCS#8 PEM or 32-byte hex signing seed.
  If the server accepts a signed \`session.logon\`, Ed25519 subsequently
  sends signed-session methods without recomputing signatures; unsupported
  deployment returns an authentication error and does not downgrade.
  Never log or persist signatures, keys, or listenKeys.
- Exact fixed-point USDT-perpetual order translation and validation from the
  immutable exchangeInfo instrument table. \`IOC\` short sell, \`GTX\`
  post-only short sell, and \`MARKET\` one-way reduce-only buy-cover use the
  appropriate LIMIT or MARKET schema. Quantity and price are **rejected**
  if their values differ from exchange-aligned increments (never silently
  change a risk-authorized order). Binance's \`reduceOnly\` field is not
  supported for Hedge Mode; this version blocks Hedge Mode until account
  mode is verified independently.
- Persistent WebSocket order API transport; request journal is written and
  fsynced BEFORE each outbound order request; any read/write timeout,
  unexpected response, or disconnect latches the socket as uncertain, with
  **no automatic resend**. The journal is create-only per process; existing
  journals cannot be loaded to resume orders. Exchange order ACKs are
  explicitly not treated as fills.
- User data stream REST \`POST /fapi/v1/listenKey\`, \`PUT\` keepalive
  every 25 minutes, and separate TLS private WebSocket with reconnect
  backoff. Mainnet private route uses the 2026 /private segmentation.
  The listenKey is never printed. On a loss or malformed packet, route
  ReconciliationRequired / Disconnected to Core 2.
- Parsing real \`ORDER_TRADE_UPDATE\` including \`c\`, \`i\`, \`t\`, \`X\`,
  \`S\`, \`L\`, \`l\`, \`z\`, \`n\`, \`N\`, \`rp\` and exchange event time;
  \`ACCOUNT_UPDATE\` includes actual USDT wallet / cross-wallet values and
  position side, signed quantity, entry price and update reason. External,
  forced-liquidation or unknown client IDs are always escalated rather
  than silently mutating local inventory.
- Fixed 64-byte \`ExecutionReportEvent\` sent through a bounded
  \`rtrb\` SPSC ring from Core 3 into Core 2; bounded auxiliary channels
  carry account snapshots and trade IDs / commission asset provenance.
  Overflow is fail-closed. No simulated/private fills.
- \`private-watch\` executable for authenticated **read-only** Testnet
  diagnostics, with no secret logging and no order submission. This command
  is optional and should be used only with a trusted, restricted API key.

## Safe configuration

\`\`\`bash
# Only if you have separately created a Testnet API key.
export BINANCE_API_KEY='testnet_key'
cargo run --locked -p trading-execution --bin private-watch -- \
  --environment testnet --symbols BTCUSDT,ETHUSDT,SOLUSDT
\`\`\`

For actual order API the library requires either
\`BINANCE_API_SECRET\` (HMAC), or \`BINANCE_ED25519_PRIVATE_KEY\`
(Ed25519 signing seed/PKCS#8 PEM), never both. Do not put secrets in
shell history or unprotected scripts.

The default socket mode is \`InspectionOnly\`; all \`place\` and
\`cancel\` calls fail with Disabled. The only experimental opt-in transport
is \`TestnetSupervised\`, requiring environment
\`TRADING_LAB_TESTNET_ROUTING=I_ACCEPT_TESTNET_ORDER_SUBMISSION\`.
**Do not enable it until Part 3 binds risk-approved intentions to
broker-verified positions, open-order reservations and a manual kill path.**
Mainnet order submissions are *not* activatable by this crate.

## Outstanding safety and correctness work (mandatory)

1. The wallet manager must reconcile signed v2 account/account positions
   before enabling any order. \`ACCOUNT_UPDATE\` is incremental (only
   affected balances and positions may be included), not a full snapshot;
   missing USDT rows cannot be zeroed or treated as fresh account equity.
2. Deduplicate fills by trade ID and exchange order ID across reconnects;
   the 64-byte report alone is not enough to prove uniqueness. The audit
   channel carries these identities. \`PARTIALLY_FILLED\`, \`FILLED\` and
   order-cancel states must reconcile with REST history and independent
   wallet snapshots after every session interruption.
3. Commission assets other than USDT, funding/interest, liquidation, ADL,
   conversion and user-initiated orders require explicit ledger entries.
   A net \`realizedProfit - commission\` calculation is valid only after
   matching the commission asset to USDT and deduplicating fills.
4. Exchange-side \`session.logon\` support and Testnet/private URLs need
   independent authenticated live test. Binance Spot and USD-M support
   are not interchangeable; this module fails if Futures rejects session
   authentication instead of sending unauthorized requests.
5. The current request journal establishes local write-before-send
   intent but has no order ACK/fill reconciliation, persistent restart ID
   allocator, MAC, authenticated account recovery, or globally atomic
   reservation service. The create-only file is a crash fence, **not**
   broker-position recovery.
6. API order frequency, timestamp drift, exchange recvWindow and time sync,
   liquidation/portfolio-margin modes, rate limits, STP, trading permission,
   user-stream gaps, time-budget cancellation, and actual emergency
   flatten semantics still need exhaustive adversarial qualification.
7. No strategy-to-execution automatic routing or live wallet mutation is
   enabled. These tasks belong to Live Upgrade Part 3.

**No release-ready or production-safe claim is made for Part 2.**
