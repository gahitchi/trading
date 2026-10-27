# Part 3 — wallet buckets, Bayesian shrinkage and capital sizing

**Scope:** integer-only, deterministic, independent research component in
\`crates/wallet-manager\`. No broker access, execution, order cancellation,
risk-gate implementation or margin settlement. All values in USD units at
scale 1e8; Z/ISR/toxicity inputs are fixed-point at the same scale.
The runtime does **not** use floating-point for wallet arithmetic.

## Main operations

- \`WalletBuckets::new(initial_equity)\`: initialize 30% reserve, 0% vault,
  and 70% actively risked equity. Reserve and vault are **logical accounting
  restrictions only**, not separately custodied cash.
- \`settle_close(id, net_realized_pnl, profit_lock_pct)\`: accept only strictly
  ascending nonzero settlement IDs, never credit duplicate closes, sweep
  configured share of positive realized PnL to the vault, and debit negative
  PnL to active equity only. PnL must be truly net of all fees, financing,
  borrow charges, slippage and funding. The wallet does not calculate PnL.
  An insolvency attempt latches a halt and returns an error requiring
  external broker reconciliation. It does not silently create negative cash
  or debit a protected bucket.
- \`cppi_damper_ppm()\`: squared cushion relative to session active-equity
  HWM, with floor 96% of the ratcheted HWM; latches \`halt_required\` on
  breaching floor. **CPPI cannot mathematically prevent gaps or liquidation**
  and does not flatten positions on its own.
- \`BayesianKelly\`: Beta(11,9) posterior with 0.99 retained prior observations;
  prior reward/risk payoff ratio of 1.50, 25%-Kelly fraction clamped to
  [0.4%,3.5%] for **strictly positive** inferred advantage, zero when
  nonpositive. R-multiples are normalized by *ex-ante loss budget*, not by
  dollars of different trade sizes. Each strategy has isolated statistics.
  30 effective closed trade observations are required before any proposal.
- \`ConvictionInputs\`: exact integer evaluation of the 0.35/0.35/0.30
  weighted formula and [0.60,1.85] clamp.
- \`compute_short_position_size\`: short-entry proposal only. Intersects
  loss-budget-derived notional using 10bps minimum / 1.2 sigma stop,
  squared CPPI damper, conviction multiplier, 25% top-5 bid liquidity,
  2.5bps static impact-slope cap, and configured gross leverage minus
  existing **and pending** gross exposure. Returns an explicit Reject
  reason for unsatisfied predicates. No order is executed by this crate.

## Why this is not yet production-qualified

1. The user's specification calls fixed Bayesian hyperparameters "Empirical
   Bayes" but does not estimate those hyperparameters from cross-sectional
   data. This code implements a fixed-prior posterior and EW payoff ratios,
   **not empirically fitted hyperpriors**. Outcomes are not IID; prior and
   warm-up window require holdout calibration.
2. The supplied \`clamp(..., 0.004, 0.035)\` would **force new risk** even
   when the Kelly edge is negative. The estimator explicitly returns zero
   in that case and refuses proposals until 30 effective observations.
3. The ten-level static price-versus-visible-depth slope is **not** a
   causally estimated Kyle \`lambda\`. Its 2.5bps cap is a research proxy;
   venue-order fill tests and real impact modelling remain necessary.
4. The price/quantity product assumes the book is denominated in USD.
   Cross-quote instruments (including USD-pegged settlement tokens),
   inverse contracts and FX must be normalized and checked *outside* this
   sizing function before deployment. Never silently equate USD and USDT.
5. A book snapshot in memory can be stale or discontinuous; this crate
   cannot verify that it comes from a synchronized live feed. Only the
   future risk gate, linked to the venue-aware data synchronizer, can
   authorize entries using a qualified quote.
6. Wallet buckets are not broker balances and have no account margin model.
   The \`MarginReserveBucket\` is not an exchange-enforced reserve; the vault
   is not protected if the broker seizes the account collateral. Order
   reservations, real fees, liquidation haircuts, maintenance margin,
   durable settlement inputs, PnL mark-to-market and funding/rebates are
   future requirements, not waived.
7. Short exits, asymmetric long-side impact, hedging/collateral netting,
   order netting, precision/lot sizing, min notional under venue filters
   and market-hours compliance must be delegated to the subsequent risk
   and execution gates.

## Verification

\`\`\`bash
cd ~/trading-rust
git pull --ff-only
cd rust
cargo fmt --all -- --check
cargo test --workspace --locked --all-targets
cargo clippy --workspace --locked --all-targets -- -D warnings
cargo build --workspace --locked --release
\`\`\`

A green unit suite verifies implemented invariants, **not** realistic
wallet survival or profitable automated execution.
