# Part 5 — independent pre-trade risk gate

**Scope:** Fixed-point, bounded in-memory risk assessment crate
\`crates/risk-gate\`. No remote credentials, exchange network, live order
submission, actual cancels, vault transfer, or broker liquidation is possible
through this crate. A passing gate verdict is **not** an execution authority.

## Implemented

- **Independent CPPI hard floor:** If active equity <= 96% of the
  ratcheted session high-water mark, or the wallet has latched a halt,
  \`monitor\` atomically sets \`TradingMode::HaltAll\` and issues a directive
  demanding cancellation, external full flatten and vault sealing. The
  risk-gate does not pretend that these actions have happened.
- **Crypto perpetuals combined short-squeeze breaker:** When the 8h funding
  rate falls **strictly below -3.0 bps** *and* perp/index basis falls
  **strictly below -15.0 bps**, opening shorts by strategy A and C are
  denied; strategy B quantity is quartered (75% reduction), using integer
  arithmetic. Strategy D still requires its explicitly armed operator
  mode and all other guardrails.
- **Confirmed in-position upward Hawkes squeeze:** Only an
  independently reconciled short inventory with ISR_up strictly greater
  than 5 and OFI Z strictly greater than +2.5 can request
  \`require_reduce_only_short_cover\`; the independent monitor halts
  new entries. Upward ISR remains a provisional model estimate from Part 2.
- **Network latency and jitter circuit breaker:** All entry eligibility
  starts blocked. End-to-end latency **strictly greater than 150 ms**
  retrips; **50 consecutive samples strictly below 25 ms** reset the
  breaker. Intermediate ticks break the recovery streak. A recovered
  breaker also requires a recently received verified packet; old healthy
  samples cannot authorize entries. Clock reversal trips it.
- **Deterministic token bucket:** Fractional nanosecond refill, bounded
  burst capacity and refusal to spend an entry token for a policy-rejected
  order. No heap allocations during normal checks.
- **Multi-asset compliance:** Equities require a reliable independent
  short locate. An active SEC Rule 201 / national-best-bid price violation
  produces an explicit rejection, **not silent repricing**, and requires a
  new fully revalidated order. Forex and commodities reject entries during
  the venue-specific central-bank/rollover blackout. The existing shared
  compliance traits in core-types are reused.
- **Exposure/market gates:** Validated operator mode, strategy mask,
  symbol mask, properly directed short sell, positive risk budget with
  stop above sell price, exposure + pending exposure bounded by
  operator/policy leverage, estimated loss <= risk budget and margin
  headroom, synchronized and timestamp-fresh venue data, nonexpired
  funding/basis for crypto, isolated paper environment, proof flags for
  broker position reconciliation and atomic reservations, monotonically
  increasing local client IDs.
- **Separate reduce-only cover assessment:** Can propose buy-to-cover under
  operator HaltAll, bounded by actual reconciled short inventory, without
  spending an entry token. This is **only a preliminary proposal** and
  the future execution gateway must still reserve aggregate pending covers
  and verify a new quote before sending. A venue blackout does not
  automatically prevent a risk-reducing cover.

## Safety boundaries and remaining architecture

1. \`RiskContext.paper_environment\`, \`reconciled\`,
   \`reservations_current\` and \`certified_event_provenance\` are **trust
   boundary assertions**. The current system has **no runtime proving**
   these assertions or transactionally obtaining a broker-validated
   reservation. A forged assertion must not make an external trade possible;
   Part 6 must provide the actual evidence binding.
2. \`RiskDirective\` describes mandatory supervisory actions. It is not an
   exchange cancellation or flatten event. Hardware/runtime failure, API
   disconnect, rejected IOC, maintenance margin and exchange halt may stop
   the intended action from succeeding. Completion requires actual broker
   fills and position reconciliation.
3. \`RiskGate\` stores its token balance, order ID high-water mark, and
   session-halt latch **only in memory**. The Part 6 execution architecture
   must durably journal order identities, enforce crash-safe idempotency,
   and obtain atomic reservations before any paper order can be sent.
4. The pre-trade "risk budget" uses a nominal price-to-stop distance only.
   It is **not** a stochastic maximum loss or a fully collateralized
   maintenance-margin / liquidation calculation. Commissions, borrow fees,
   funding, gap risk, price impact, basis risk and netting rules remain
   unmodelled here.
5. Fixedpoint quote notional is USD **only for USD-quoted instruments**.
   FX, equities in other currencies, linear USDT perps, inverse contracts,
   and collateral haircuts require independently qualified conversion
   and account-model adapters before route authorization. A USDT notional
   must not be silently treated as USD cash collateral.
6. A stored packet timestamp cannot alone establish clock accuracy,
   exchange event provenance or valid market microstructure. Part 6 must
   qualify gateway messages, timestamp normalization, sequence continuity,
   instrument symbol mapping, authenticated operator control, country/
   venue rules, short locate records, and actual paper credentials.
7. The requested **less-than-1-microsecond** worst-case validation bound
   is a performance target, **not verified** by the unit suite; it needs
   pinned-core Arch benchmarks and a latency-distribution report under
   representative concurrent load, including tail latencies.
8. The risk gate's in-memory veto does not replace globally routed circuit
   breakers or a real account-kill/flatten service. Hazard monitoring must
   run independently of any strategy activity.

## Validation

\`\`\`bash
cd ~/trading-rust
git pull --ff-only
cd rust
cargo fmt --all -- --check
cargo test --workspace --locked --all-targets
cargo clippy --workspace --locked --all-targets -- -D warnings
cargo build --workspace --locked --release
\`\`\`

All tests are deterministic unit/invariant tests. Complete exchange adapter
replay, fuzz/property testing, borrow/SSR historical scenarios and real
broker-collateral policy remain mandatory before any paper broker workflow.
