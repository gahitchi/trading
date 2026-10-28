# Zero-budget Rust venue qualification

**Goal:** Build a demonstrable product without trading capital, paid data, hosted GPU or a Binance account. No production order permission changes.

## Environments

| Environment | REST | Public WebSocket | Order-API WS |
| --- | --- | --- | --- |
| Mainnet (read-only by default) | `https://fapi.binance.com` | `wss://fstream.binance.com/public` + `/market` | `wss://ws-fapi.binance.com/ws-fapi/v1` (submission sealed) |
| Legacy Testnet | `https://testnet.binancefuture.com` | `wss://stream.binancefuture.com` | `wss://testnet.binancefuture.com/ws-fapi/v1` |
| Demo | `https://demo-fapi.binance.com` | `wss://demo-fstream.binance.com` | Uses non-production Futures WS API host pending venue validation |

Demo is not an alias for Testnet: select `--environment demo` in the read-only `market-watch` and `private-watch` CLIs. No Demo credentials are required for public data. The order API path is NOT considered authenticated-validated. It is disabled by the existing operator/risk gates.

## Local protocol lab

`crates/protocol-lab` is a **separate test-only binary and library** that binds ephemeral ports on `127.0.0.1` and exercises a Binance-shaped WS API and distinct private WS feed. It never contacts the internet or changes the production connector.

The fixture validates canonical HMAC-SHA256 requests, rejects tampered signatures and repeated order IDs, returns an order ACK independently of execution, and emits deterministic `NEW`, partial `TRADE`, `EXPIRED`, and incremental `ACCOUNT_UPDATE` payloads. The existing real-exchange private parser parses the mock frames in the smoke test.

**All synthetic fills originate exclusively inside this local testing component.** They are not data for live signals, broker reconciliation, equity or production execution. The fixture cannot prove live Binance authentication or private-stream availability.

Commands for Arch or Termux:

```bash
cd ~/trading-rust-termux/rust   # or ~/trading-rust/rust on Arch
cargo test --locked -p trading-protocol-lab --all-targets
cargo run --locked -p trading-protocol-lab --bin protocol-lab
cargo run --locked -p trading-market-data --bin market-watch -- \
  --environment demo --symbols BTCUSDT,ETHUSDT,SOLUSDT
```

The first two commands are deterministic and require **no account or API key**. The last uses real read-only public Demo connectivity and can fail if Demo networking is unavailable in the user's region; it is not a trading failure.

## Qualification still required before funding / real release

- Supply strategy-to-risk-to-order-to-exchange integration using the same production code path; do not ship an investor demo masquerading as a live authenticated trading account.
- Add causal private-stream/REST snapshot fence, exchange order- and trade-history backfill, persistence, rate-limit/clock correction and independently reconciled margin/funding, commissions and positions.
- Exercise order placement/cancel races, uncertain ACKs, unexpected disconnects, duplicate fills, latency, memory pressure, and fault injection in the isolated venue.
- Build an operator-facing plug-and-play UI, run 24-hour soak tests on Arch and ARM64, and qualify a supervised no-money exchange account if available.
- Continue respecting `COLLABORATION.md` paper-only guarantees. Only deliberate future policy review and owner approval could permit live Mainnet order routing.

## References

Binance Developer Documentation: https://developers.binance.com/en/docs/introduction

NautilusTrader Binance connector environment comparison: https://nautilustrader.io/docs/latest/integrations/binance/

Demo public endpoints were verified against connector documentation; no authenticated Demo order-WS endpoint has been empirically qualified in this branch.

## Shared production order-response qualification

The isolated protocol lab now exercises \`trading_execution::decode_order_response\`,
the same decoder used by the real \`ExchangeSocket\`. It strictly binds a
successful response to its expected request ID, symbol, client order ID
and positive exchange order ID. It never treats an ACK as a fill.

Explicit non-ambiguous 4xx errors produce \`Rejected\`. Backend timeouts
(\`-1006\`, \`-1007\`), 5xx, duplicate-client-ID responses (\`-2010\`),
and rate limits produce \`Uncertain\`. The production socket latches
into its halted state on uncertainty and does not resend. Missing fields,
unexpected IDs, and malformed responses also halt the socket.

This is a protocol validation milestone, not production order readiness.
The local test fixture still does **not** call the full production
\`ExchangeSocket::place\` networking path; that requires further isolated
socket injection and durable journal recovery verification.


## Production socket integration — localhost-only unit harness

The \`trading-execution\` crate now runs the **actual production
\`ExchangeSocket::place\` and \`ExchangeSocket::cancel\` methods** through
real loopback WebSockets inside \`#[cfg(test)]\`. The synthetic
authentication fixture constructor exists only in unit-test builds. It
never changes the production host-selection code or its Mainnet/Demo
order-submission gates.

The isolated fixture validates the HMAC on each request independently,
requires exactly specified order fields, and checks the crash-fence
intent journal actually persists **before** order acknowledgement.
It also proves that a second session refuses an existing journal.
Scenarios cover successful place+cancel, unambiguous rejection followed
by safe continued operation, uncertain exchange timeout (permanent
socket quarantine and no resend), mismatched request ID, and abrupt
disconnect. The local fixture is the only origin of synthetic responses.

This proves the production **request path** can execute on Termux and
Linux in an isolated test. It does not yet establish external
authenticated Binance account availability, wallet settlement, full
strategy/risk integration, or crash-time order-history backfill. The
new tests intentionally avoid an API URL override in production builds.
