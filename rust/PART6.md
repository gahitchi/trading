# Part 6 — integrated **offline synthetic paper** execution

The six-part Rust research foundation now includes a deterministic paper-only
runtime under \`crates/paper-gateway\`, with a CLI binary \`paper-lab\`.

**Scope boundary:** This is **not** a live broker gateway, authenticated
Binance/Bybit connector, real-money execution environment, or calibrated
historical backtester. There are **no network clients** in this crate.
All exchange information, short locates, quotes and account reconciliation
are **synthetic fixtures**. It cannot be repurposed for live order routing
without developing and reviewing an entirely separate broker adapter.

## Run the self-contained example (safe; no credentials)

\`\`\`bash
cd ~/trading-rust
git pull --ff-only
cd rust
cargo run --locked --release -p trading-paper-gateway --bin paper-lab -- demo "$HOME/trading-paper-$(date +%s%N).journal"
\`\`\`

Use the actual journal name printed in your command when inspecting:

\`\`\`bash
cargo run --locked --release -p trading-paper-gateway --bin paper-lab -- inspect ./paper-session-....journal
\`\`\`

\`demo\` refuses an existing journal path. Each new run requires an explicit
new file. The CLI creates the file, warms up a synthetic 50-packet stream,
arms the local operator interlock, passes a synthetic **Part 4 Strategy A entry candidate** through the
**Part 5 RiskGate**, updates Part 4's per-order shadow FSM on local
acknowledgments and fills, records the reserved order, applies only a
**0.6-unit simulated
fill** to a 1-unit short, journals the cancellation of the remainder, then
places a **reduce-only simulated cover**, applies two cumulative fills of
0.4 and 0.2 units, records net realized paper PnL, and seals the flat
journal. After every append the new state is applied only after \`sync_all\`.

\`inspect\` only reads, checks the journal and reconstructs positions,
pending orders, simulated realized PnL and the Part 3 wallet from durable
events. It cannot resume a crashed order or submit one to a broker.
An unfinished journal remains **inspect-only with unresolved orders**.
A truncated, invalid or noncanonical journal is rejected.

The in-process Part 4 `OrderTracker` checks paper ACK / partial-fill /
cancel-confirmed transitions and cumulative fills. After a crash, only the
durable journal can be reopened in **inspection mode**: no reconstructed
tracker is permitted to resume sending anything.

The journal uses a versioned text header, monotonic event sequence, simple
FNV-1a chained checksums, strict event transition validation and bounded
length. The checksum detects accidental damage, **not malicious tampering**:
it is neither a MAC nor a digital signature. Protect journal files with
normal OS permissions and store them on a filesystem offering durable
\`fsync\` semantics. Hardware storage caches and unusual filesystems may
not honor durability assumptions.

## Combined Part 5 and Part 6 qualification

\`\`\`bash
cd ~/trading-rust
git pull --ff-only
cd rust
set -o pipefail
(
  cargo fmt --all -- --check &&
  cargo test --workspace --locked --all-targets &&
  cargo clippy --workspace --locked --all-targets -- -D warnings &&
  cargo build --workspace --locked --release &&
  cargo run --locked --release -p trading-paper-gateway --bin paper-lab -- demo "$HOME/trading-paper-combined-$(date +%s%N).journal"
) 2>&1 | tee ~/trading-rust-parts5-6-arch.log
\`\`\`

The user may move the generated \`paper-combined-...\` journal into a
different diagnostics folder. Do not commit journals as repository files.

## Architectural gates still missing for a fully viable product

1. **No actual supervised external paper broker trading.** Implement a
   broker/exchange-specific adapter and reconcile signed venue/order/position
   records against independent account state. RiskContext paper flags in
   Part 5 are currently constructed from local *simulated* account state.
2. **No whole-system qualified market ingestion**, production instrument
   normalization or independently proven 50ms freshness from a venue.
   The demo stream is synthetic and never asserts outside observations.
3. **No durable multi-order concurrent reservation manager.** This demo
   permits a single outstanding paper order and one USD-denominated short
   instrument, refusing additional entry or cover while one is pending.
   It cannot expose self-trade prevention or venue order queue position.
4. **No order lifecycle networking**: expected IOC cancel/fill races are
   modeled locally and verified by tests, not by exchange messages.
5. **No measured <1µs worst-case latency guarantee.** The journal
   intentionally fsyncs and is not a high-frequency hot path.
6. **No operational GUI, advanced installer, connected data subscriptions
   or strategy parameter calibration.** The standalone Rust workspace has
   not replaced or merged into the Python MVP.
7. **Paper outcomes are not strategy performance.** A deterministic
   round-trip with positive simulated PnL provides no evidence of
   profitable alpha or robustness to spread/slippage/fees/borrow/gaps.
8. The risk gate issues supervisory directives, but this offline demo has
   no independently managed kill-and-flatten service; an actual broker
   adapter would need to implement these before live or broker paper use.

Do **not** call this release-ready or institutional production software.
It is a qualified integration *prototype* once CI and independent Arch
tests pass; real market validation and operational interfaces remain.
