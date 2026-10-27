# Part 2 — deterministic microstructure research signals

**Status:** Research-only alpha calculators. No venue connectivity, wallets, risk
gate, executable order intents or calibrated predictive signal thresholds.
This module is deliberately separable from the legacy Python stack.

## Included engines

| API | Input | Output | Limits and interpretation |
|---|---|---|---|
| \`MlOfiEngine\` | Timestamp + validated 10+ level book | 10-level, spatially weighted OFI, EWMA Z | Level-ranked approximation; not an individual-order event attribution |
| \`MicroPriceEngine\` | Timestamp + validated 5+ level book | Microprice, imbalance, depth/vacuum | 20bp depth is rejected if the retained book is truncated |
| \`VpinToxicityEngine\` | Aggressor-labelled positive quote notional | 50 completed volume-bucket averaged absolute/signed imbalances | Aggressor-sign toxicity *proxy*, not a statistically verified VPIN estimator; incomplete buckets are withheld |
| \`HawkesCascadeEngine\` | Monotonic timestamp, classified event, fixed quote notional | Down/up channel intensity ratios, acceleration, activity-weighted branching proxy | Supplied fixed coefficients are supercritical; proxy **not** an estimated branching spectral radius. Upstream direction must be independently observed |
| \`AbsorptionEngine\` | Timestamp, best bid, actual aggressive sells, past-only baseline | Decayed sell pressure per bid movement | Candidate replenishment/absorption proxy, not evidence of an iceberg or whale |
| \`KalmanLeadLagEngine\` | Event-time-ordered BTC/laggard prices | One-to-one overlap-gated log-return beta and residual | **Not a full Hayashi–Yoshida estimator.** True HY needs all overlapping pairs, violating strict O(1) without constrained sampling |

## Rules and gates

- No order submission, keys, network calls, order book mutation, or wall-clock access.
- Fixed-point quote/quantity inputs; isolated recursive floating-point signal
  registers only. A signal result has no authority to bypass a risk check.
- No heap allocations on updates. Initialization allocates only the pre-existing
  book arrays; the signal engines use fixed-size arrays and scalar state.
- Timestamp reversal returns an explicit error; leading/lagging streams must be
  merged in **event-time order** and must not interpolate future ticks.
- **Caller responsibility:** accept book updates only after valid batch-level
  synchronization; call the engines on the *same* instrument/basis and reset
  them after resnapshot, any detected feed gap or changes of data semantics.
- \`VpinToxicityEngine\` requires 50 completed buckets for a valid reading.
  A single arbitrarily large trade never loops over more than 49 completed
  buckets individually.
- A larger book, calibrated time windows, rolling-volume estimator, historical
  research calibration and unit-specific cross-venue normalization are required
  before any use of these numbers to govern entries.

## Run

\`\`\`sh
cd rust
cargo fmt --all -- --check
cargo test --workspace --all-targets
cargo clippy --workspace --all-targets -- -D warnings
cargo build --workspace --release
\`\`\`

All Part 1/Part 2 tests are deterministic and native-Android/Termux-friendly.
Performance, memory-allocation assertions under a tracking allocator, realistic
market-data fixture replay, out-of-sample predictive power and calibration
remain outstanding; they must precede paper-broker tests.

## Mathematical discrepancy register

1. The given rank-indexed 10-level OFI contribution formula does not faithfully
   reconstruct every add/cancel/depletion during simultaneous level shifts.
2. Sign-labelled trade flow is a reasonable experiment but differs from the
   historical bulk-volume classification used in the original VPIN literature.
3. The supplied \`rho_eff\` expression is a bounded activity proxy, not the
   spectral radius of the 2x2 Hawkes kernel. Under the supplied triangular
   coefficients the theoretical spectral radius is \`max(3.2/8, 6.5/2.5)=2.6\`;
   this is unstable for a stationary linear Hawkes process. Calibrate a
   *subcritical* model on real data before using branching thresholds.
4. The specified upward ISR has no underlying upward-event equations. The
   implementation uses explicit separate buy and forced-short channels with
   the same provisional coefficients, and labels the result as a proxy.
5. Iceberg status is not observable from L2 prints, and 400ms EWMA price
   displacement is only a detector heuristic, not proof of hidden support.
6. The supplied Kalman return regression alone does not implement the
   Hayashi–Yoshida asynchronous covariance integral. We avoid that label
   for the O(1) one-to-one nonlookahead overlap-gated estimate; a fully
   specified HY estimator is a separate research work item.
