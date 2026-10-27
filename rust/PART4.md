# Part 4 — strategy triggers, short lifecycle, quoting, order status

This workspace is a **research-only, deterministic, short-side decision library**.
It does not execute, route, reserve, authorize, send or cancel orders, or
infer exchange fills from quotes. The Python trading application is unchanged.

## Delivered

- \`EntryEngine\` implements the four strategy A/B/C/D *candidate conditions*
  against explicitly synchronized, timestamp-bounded signal snapshots.
  It honours operator mode, enabled strategy mask, active symbol mask and
  aggressiveness in the [0.5,2.0] interval. Candidate priority is B > C > A.
  Strategy D requires an explicit per-symbol authenticated-arm **input
  supplied by the future operator gateway**, with arm ID, threshold and
  absolute expiry; arms are consumed once. The engine does not itself verify
  operator authentication.
- \`choose_short_quote\` compares the documented expected short alpha urgency
  heuristic to 1.5 times the bid/ask spread. It returns a proposed IOC sell
  limit at most four bps below the best bid, or a passive sell limit at or
  above the best ask. Venue tick rounding, precision, freshness, STP and
  maker/taker checks remain outside the module.
- \`ShortTradeLifecycle\` is created *only* from externally confirmed short
  position inventory. It maintains phases InitialRisk, Breakeven,
  PartialExitPending, Runner, ExitPending, Closed and ReconciliationRequired.
  It supports fakeout abort, fee-aware short breakeven ratchet, half-position
  harvest, fill-confirmed runner activation, volatility trailing stop,
  absorption and stagnation exits. Phase transitions that imply exposure
  changes are dependent on acknowledged cumulative fills rather than a
  presumed fill after submitting an order.
- \`OrderTracker\` records a single order's Draft/Sent/Acknowledged/
  CancelPending/Filled/Canceled/Rejected/Unknown state, cumulative fills,
  authoritative per-order source sequencing and exact replay protection.
  Fill reports during CancelPending are accepted as races, and unsupported
  transitions fail closed. The future venue adapter must provide genuinely
  monotonic per-order event sequence numbers; not all exchange message fields
  meet that contract.

## Model limitations, deviations and blockers

1. The supplied **HJB / Snell Envelope** formula is a weighted \`tanh\`
   heuristic, not an optimal-stopping PDE or empirical conditional value
   estimate. We retain the formula as \`continuation_proxy\` and explicitly
   do not market it as a solved optimal-stopping policy.
2. The provided IOC/post-only thresholds do not solve **Almgren–Chriss**
   market impact or **Avellaneda–Stoikov** inventory-dependent optimal
   quoting. The implementation labels them \`QuoteDecision\` heuristics.
3. The provided Hawkes activity number is not a fitted branching spectral
   radius. Strategy B's 0.8 and phase continuation 0.65 thresholds are
   provisional inputs, *not proven indicators of a liquidation cascade*.
4. Automatic **35% house-money pyramiding is not implemented**. Its claimed
   zero wallet risk cannot be guaranteed by a unified stop: market gaps,
   liquidation, funding and execution costs make this impossible. A future
   authorized add-on would have to pass the independent risk gate and
   liquidity/leverage reservations, including the new exposure, first.
5. Stops are **intended limits to loss**, not exchange-protected executable
   orders. Quotes do not guarantee fills, and a short's fee-adjusted stop
   below the entry is not a guarantee of breakeven. Exchange-enforced
   reduce-only market/IOC exits are required at the gateway.
6. A pending close cannot yield another independent cover proposal.
   Crossing a stop with an unresolved close raises \`PendingExitAtRisk\`
   requiring immediate supervisory recovery. An unknown/canceled full
   close latches ReconciliationRequired; it cannot silently resume.
7. Every Phase 2 partial close requires a terminal per-order report with
   cumulative executed quantity. Cancel-ack races must be resolved by the
   venue adapter before generating the next remaining-quantity proposal.
   Local decision IDs are not globally unique exchange order identifiers.
8. **No venue account reconciliation, durable order-intent journal,
   pending exposure reservation, authenticated arming, execution gateway,
   venue tick/lot filters, market hours, funding/basis breakers, forced
   position flatten, external risk gate or broker/PnL integration** is
   included in this milestone.
9. The source requires **synchronized event-time-ordered market data** and
   uses strict 50ms maximum age for each merged decision snapshot. The
   \`feed_live_and_synchronized\` flag is a caller assertion, not an
   exchange-proven data-quality certificate.
10. No empirical out-of-sample validation or realistic fill replay has
    qualified the trigger thresholds, half-position cashouts, or ratchet.

## Verification

\`\`\`sh
cd ~/trading-rust
git pull --ff-only
cd rust
cargo fmt --all -- --check
cargo test --workspace --locked --all-targets
cargo clippy --workspace --locked --all-targets -- -D warnings
cargo build --workspace --locked --release
\`\`\`

Passing compile/tests is the implementation gate only. Runtime integration
remains blocked until an **independent** Part 5 risk gate, a durable Part 6
venue execution gateway, and research-quality event replay qualify it.
