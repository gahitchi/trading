# Live Upgrade Part 3 — Read-only account observation

Adds signed REST bootstrap for Binance USD-M account data, open orders and position mode. Adds a fail-closed private-event reconciler with trade-ID deduplication and partial account updates. This is only the first slice of Part 3: no order routing, wallet settlement, operator arming or live trading is enabled by these components.

The first Termux acceptance gate is `cargo test --locked -p trading-execution --all-targets`. Optional signed Testnet account inspection uses `cargo run --locked -p trading-execution --bin account-inspect -- --symbols BTCUSDT,ETHUSDT,SOLUSDT` with a restricted local Testnet API key and signing secret. Do not share credentials.

Remaining hard blockers: private-stream-first snapshot fencing and clock synchronization, exchange order-history reconciliation, funding and fee-asset conversion, durability across restarts, authenticated account/margin mode validation, kill/flatten service, performance qualification and independent paper-account acceptance. `AccountReconciler::trading_permitted()` intentionally remains false.
