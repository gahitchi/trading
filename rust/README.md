# Rust Trading Engine — Parts 1–5 of 6

**Status:** Parts 1–3 passed Linux CI and independent Arch validation
(61 total tests at Part 3); Part 4 passes Linux CI (91 total tests), with
independent Arch and Termux validation still pending. This is a **research
foundation**, not a live trading application. The Rust workspace lives under `rust/` alongside, but remains
fully isolated from, the legacy Python trading-lab application. No exchange
keys, external orders, accounts, or live venue connections are present.

## Delivered

- Root `Cargo.toml`: 2024-edition workspace and shared dependency catalog.
- `crates/core-types`: exact 64-byte aligned `MarketEvent` and `OrderIntent`,
  checked signed i64 / i128 fixed-point arithmetic at 1e8 scale, exact decimal
  parser, packed Acquire/Release operator state, compare-and-swap updates,
  bounded non-blocking command endpoints, order / venue types and preliminary
  compliance policy hooks.
- `crates/orderbook`: preallocated sorted bid/ask arrays, insert/update/delete,
  capped 1,000-level retention, 20bps depth aggregation with truncation checks,
  ten-level depth-slope proxy, 30-level liquidity-wall search, and explicit
  Disconnected / FetchingSnapshot / Validating / Live synchronization FSM.
- `crates/signals`: all six Part 2 microstructure research registers, with
  bounded state and deterministic unit tests. See [Part 2 implementation
  and mathematical limitations](PART2.md).
- `crates/wallet-manager`: three-bucket fixed-point wallet, session HWM/CPPI,
  per-strategy fixed-prior fractional Kelly, conviction weighting and bounded
  short-side sizing. See [Part 3 documentation](PART3.md).
- `crates/strategy-engine`: Four entry-condition probes, bounded manual-armed
  breakdown trap, IOC/post-only quoting heuristics, short-side four-phase
  lifecycle with cumulative-fill accounting, and per-order shadow FSM.
  See [Part 4 documentation](PART4.md).
- `crates/risk-gate`: independent fixed-point pre-trade gate, network latency
  trip/recovery circuit, CPPI/squeeze supervisory directives, funding/basis
  breaker, quote/exposure/borrow checks, and token-bucket throttling.
  See [Part 5 scope and safety limitations](PART5.md).
- Unit tests cover arithmetic, layout, bitfields, order book invariants,
  buffer overflow, crossings, sequence disconnections and signal contracts.

## Test locally

Install stable Rust >=1.85 on your Arch Linux host and, from this folder:

```bash
cargo fmt --all -- --check
cargo test --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo build --workspace --release
```

Cargo downloads dependencies on the first build. Keep `rust/Cargo.lock`
tracked and use `cargo test --workspace --locked --all-targets` once the
lockfile is committed. Release review also requires architecture-specific
benchmarks, property/fuzz tests, and independent venue-fixture validation.

## Material corrections to the provided specification

1. `MarketEvent` has six 8-byte fields and five metadata bytes: **53 bytes**.
   Exactly **11**, not 19, padding bytes are required for 64-byte layout;
   compile-time const assertions enforce the size and alignment. `OrderIntent`
   is separately laid out and compile-time-checked for exactly 64 bytes.
2. No floats are used in arithmetic, book prices, quantities or notional. All
   conversions reject unrepresentable values rather than truncating downcasts.
   Multiplication and division use truncation toward zero; execution rounding
   must subsequently satisfy market-specific tick and lot rules.
3. **Binance USD-M Futures** uses `U`, `u`, and the exchange-supplied `pu`
   field. The first processed USD-M Futures update bridges the snapshot via
   `U <= lastUpdateId <= u` (this differs from spot). Events with
   `u < lastUpdateId` are dropped. Once live, `pu` must match previous `u`.
   Without `pu`, continuity is NOT proven, and entry is blocked.
4. **Bybit V5 orderbook (standard)** supplies WS snapshots, `u` and `seq`.
   Monotonicity is checked, but nonconsecutive `u` is not by itself treated as
   a gap for normal depth streams, because it is not documented as a contiguous
   per-message counter for those streams. Reconnection and `u=1` reset require
   re-synchronization. A plain 64-byte MarketEvent **cannot preserve all this
   sequence metadata**; the adapter MUST deliver `DepthSequence` per batch.
5. **Bybit full-depth** is a separate delta-only stream. It uses consecutive
   `u` values and requires a buffered (u, seq) anchor matching the REST snapshot.
6. “Kyle’s lambda” in the prompt is a **static book slope proxy** with units
   bps/USD; it is not an empirically estimated Kyle market-impact regression.
   The returned type is `FixedBpsPerUsd`, not USD.
7. Book inserts are bounded linear-time moves across 1,000 levels, not true
   asymptotically O(1). Best bid and ask access is O(1). No book data buffer
   grows beyond its startup capacity.
8. A limited-depth book can omit levels below the retention boundary. If the
   retained last level lies inside the target 20bps interval, the depth metric
   returns `TruncatedDepth`, rather than reporting incomplete data as fact.
9. An exchange timestamp normally uses the exchange wall clock, while
   `CLOCK_MONOTONIC_RAW` is a separate local clock domain. **Never directly
   subtract them for network latency** as the proposed breaker suggests;
   implementation needs clock synchronization or a clock-domain conversion
   with uncertainty bounds. Nanosecond units do not imply nanosecond accuracy.
10. The proposed default `TradingMode::ShortOnly` does NOT authorize live
    trading; a heartbeat register is not authentication. Execution and risk
    gating must separately require a fresh authenticated operator session,
    qualified book, venue trading status and reconcilable account state.
11. SEC Rule 201 and locate interfaces are **preliminary policy hooks**.
    They are not a complete broker-, jurisdiction- and order-type-aware
    securities compliance system. IOC/FOK shorts that would require SSR
    repricing are blocked, not silently transformed into nonmarketable orders.
12. The prompt's vault guarantee, "risk-free" stops, hard CPPI loss guarantee,
    fixed sub-microsecond risk-validation claims, and HJB identification are
    unverified or mathematically overstated. None is claimed by Part 1.

## Boundaries of Part 1

No market-data ingress adapter, SPSC inter-core runtime, execution gateway,
order router, strategy, risk engine, backtester, UI, exchange authentication,
or synthetic end-to-end test is implemented yet. Future work must test actual
venue payloads and snapshots with exchange fixtures and instrument metadata.

`cargo test` passing would verify only the included unit-test assertions;
production readiness requires deterministic fault injection, property/fuzz
checks, sanitizer/memory testing, perf benchmarking, reconnect and gap testing,
chronological backtesting, capital-account parity and supervised paper execution.

## Part 2 implemented (research only)

`crates/signals` contains the OFI, microprice/vacuum, signed volume-clock
toxicity, Hawkes cascade proxies, bid absorption diagnostic, and asynchronous
overlap-gated Kalman beta. See [PART2.md](PART2.md) for detailed limitations,
including why the supplied Hawkes activity proxy is not a fitted branching
spectral radius and why a strict O(1) overlap-gated Kalman update is not full
Hayashi–Yoshida covariance. No signal alone authorizes a trade.
