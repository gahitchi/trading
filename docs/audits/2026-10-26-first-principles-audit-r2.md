# Trading Lab — first-principles logical audit

**Date:** 2026-10-26  
**Repository:** `gahitchi/trading`  
**Inspected revision:** `chatgpt/market-data-evidence-20261026` (includes the current US-equity `main` baseline and unmerged crypto work)  
**Scope:** logic and economic invariants before further Arch acceptance, algorithm sophistication, or external demo execution.  
**Disposition:** **NOT institutionally release-qualified.** This is a source-oriented, focused first-principles review, not a completed whole-repository line-by-line audit, independently run repository CI, or live broker certification.

**R2 correction note (26 October 2026):** This revision corrects the Pandas index-unit assumption in FP-05, distinguishes intentional custom-profile scientific screening from the deployment-bound promotion mismatch in FP-02, incorporates the existing equity reservation proof when discussing crypto risk, and specifies how delisting settlements must reconcile with NautilusTrader's internal account/portfolio. The independent assessment confirmed the 71-hour DST interval and related effective-rate arithmetic. The source findings have not thereby been fixed in code.

## 1. Non-negotiable principles, in dependency order

1. **Provenance before prediction.** For each fact, keep instrument, venue, economic meaning, source, exchange event time, local receipt/availability time, corporate-action revision and completeness state. Unknown/late/stale sources must not be silently promoted into decision-quality evidence.
2. **Economic hypothesis before algorithm.** Every proposed trade must carry a falsifiable thesis, instrument/holding horizon, price and fundamental inputs available at decision time, plausible gross edge, and a no-trade alternative. Named models and sophisticated signals are not evidence of positive expectancy.
3. **Costs and uncertainty before expected profit.** Ex-ante opportunity must be compared against spread, slippage, commission, funding, borrow, market impact, fees, turnover, latency, selection error and plausible forecast uncertainty. If the net edge cannot be defended, abstain.
4. **Preserve capital before optimizing returns.** Explicit operator limits must constrain profiles and automatic policy. Include current holdings, all pending orders and their fill states, settlement obligations, stale data, cross-asset correlation, liquidity and tail stress. Models cannot lift hard caps.
5. **Conserve economic value in the ledger.** Position transitions, realized and unrealized PnL, collateral, fee charges, funding, corporate actions, foreign currency conversion and actual fills must reconcile without invented activity or double counting.
6. **Orders are not fills.** Separate trade intent, broker acceptance, acknowledgment, partial fill, cancellation, late fill, unknown state, settlement and reconciliation. A crash or stale broker view must not trigger unverified new exposure.
7. **Prove before promotion.** Null tests, selection-aware trials, untouched holdout, walk-forward evaluation, robustness, stress and prospective demo outcomes are prerequisites for *claiming strategy qualification*, not guarantees of profit.
8. **Default to no trading under uncertainty.** No signal, missing market data, missing permission, no accountable margin/funding, broken state or incomplete tests means `ABSTAIN` / `HALT` / restricted reductions, never invented substitute evidence.
9. **Keep one accountable execution path.** Reuse Nautilus, qualified broker adapters and independent risk/ledger validators; do not fork parallel production execution engines that disagree on order/account state.
10. **Auditability and human control.** Explicit market, paper/demo mode, risk budget, start/stop authority, current positions, cash, outstanding orders, data health, strategy provenance and explanations are visible before permitting autonomous operation.

## 2. Findings: confirmed source-level defects and contradictions

### FP-01 — BLOCKING (P0): fictitious post-delisting liquidity and price

**Evidence:** `src/lab/backtest.py`, `settle_delistings()`, near lines 122–163. It creates *two* post-delisting OHLC bars at the designated settlement price and sets their volume to `float(df["volume"].max())`, so the ordinary signal/execution path can simulate trades **after normal trading has ceased**. It also enforces `price = max(price, 0.0001)`, inventing a positive transaction value for a genuine zero-recovery settlement. The origin and return of any assumed distress settlement appear in quality reports, but disclosure does not fix fictional executable volume.

**Why this violates a principle:** settlement is a cash/corporate-action event, not a guaranteed market order fill with historic maximum volume. Liquidity and PnL can become fictitious.

**Required correction:** Preserve the necessary **NautilusTrader Portfolio/Cache/account parity** while replacing fabricated market liquidity. The existing two bars are a compatibility mechanism: Nautilus normally reflects quantities via the order/fill lifecycle, so simply crediting `ExecutionAgent.cash` in Python while leaving the engine's position intact would break `ledger_vs_account` and ownership/risk consistency. Implement an explicitly classified corporate-action/settlement path that updates **both** the native engine's position/account state and the independent cash/position ledger, with an auditable zero-recovery write-off where appropriate. A tightly scoped internal settlement fill is one possible adapter *only if* the pinned engine supports its price and state semantics; an ordinary market order consuming invented book volume is not acceptable. If zero-priced fills are rejected by the engine, implement a separately verified engine account/position adjustment or explicitly model settlement payable/write-off; **do not fake a positive market price** and do not weaken the parity check. 

Prevent strategy entry signals, ordinary liquidity assumptions and market impact from using any synthetic settlement markers. Preserve actual delisting/settlement dates, source, availability, uncertainty and cash-merger/short-liability treatment; assumed recoveries are stress scenarios, not executable prices. Add zero-recovery, short liability, delayed payment, absent evidence, duplicate settlement and native-ledger parity regressions. The exact settlement mechanism must be confirmed against the pinned Nautilus version, not inferred from a generic API claim.

### FP-02 — BLOCKING (P1): custom-profile research/paper promotion context mismatch

**Evidence and intended design:** `lab.toml` defaults to `profile = "balanced"`. `src/lab/experiments.py:effective_risk()` leaves **named profiles** untouched. Only `profile = "custom"` applies the documented legacy scientific-screening convention `max_weight=1.0, max_drawdown=0.9999`. The `README.md` scientific protocol intentionally removes per-stock caps and circuit breakers to avoid distorting single-/few-stock signal-vs-control comparisons. `tests/test_risk_profiles.py` asserts this behavior, so it is **not** an unrecognized default-policy bug.

**Confirmed integration defect:** `src/lab/research/worker.py` binds `deployment_context(replace(cfg, risk=effective_risk(...)).snapshot(), ...)` into its research evidence; `src/lab/paper.py:build_node()` verifies `deployment_context(cfg.snapshot(), live=True, ...)`, which keeps the **real configured custom risk**. Since the context includes `risk`, the two differ for normal custom settings. `verify_approval()` should reject such a custom-profile promoted candidate because the hashes do not match. This is primarily **fail-closed deployment unavailability**, *not* proof that the paper node silently bypasses operator risk limits. Any capital/universe/other context differences must also be evaluated separately.

**Required correction:** retain unrestricted scientific signal screening as an explicitly **non-deployable research mode**, if desired, with its own digest and control comparison. To promote a candidate, perform a second qualification run under the **exact intended deployment risk, capital, costs, venue capability and portfolio policy**; bind that approval to what `paper.build_node` actually enforces. Alternatively require identical configured risk during the deployable research path, without changing the scientifically useful exploratory protocol. Never "fix" the mismatch by silently applying relaxed research caps to paper. Add custom/named-profile approval parity and negative tests; treat existing affected approval records as requiring requalification.

### FP-03 — BLOCKING (P1): explicit liquidity cap fails open with limited history

**Evidence:** `src/lab/agents/execution.py`, `_liquidity_cap()` near lines 56–61. If participation is configured but fewer than five volume bars exist, the function returns `None` (“no cap”), and the rebalance path near lines 928–935 applies no liquidity limit.

**Why:** missing liquidity data is not proof of unlimited liquidity. Capital and per-order limits still exist, but they do not guarantee realistic capacity.

**Required correction:** distinguish disabled policy (`None`) from insufficient observations (`0` opening capacity or reject new exposure). Preserve risk-reducing actions. Test zero/one/four/five observations, stale bars and exits during a feed outage. Do not assume average-bar participation equals actual broker fills.

### FP-04 — BLOCKING (P1): paper/backtest liquidity-policy discrepancy

**Evidence:** `src/lab/backtest.py`, near line 400, passes `max_participation=cfg.costs.max_participation` to `ExecutionAgentConfig`. `src/lab/paper.py`, `build_node()`, constructs `ExecutionAgentConfig` without passing the same field, leaving it at the `None` default. The participation window similarly defaults instead of being explicitly bound to the researched setting.

**Why:** a capacity assumption used to produce a strategy result is absent from the corresponding paper execution policy. Mode differences must be intentional, documented and independently verified, never accidental.

**Required correction:** bind supported paper controls from the exact researched configuration or deny promotion if venue data/permissions cannot support them; add configuration parity tests and check real delayed-volume semantics.

### FP-05 — DEFECT (P2): exact elapsed time truncated in risk-free interest accrual

**Evidence:** `src/lab/metrics.py`, `excess_returns()`: `np.diff(equity.index.to_numpy()).astype("timedelta64[D]").astype(float)` floors elapsed days. Independent NumPy/Pandas reproduction with New York closes at 2026-03-06 16:00 EST and 2026-03-09 16:00 EDT: 71 hours / 24 = 2.958333 days, counted as 2 days. At 5% annual effective rate, expected return on cash over the interval is 0.0003955236 versus the current 0.0002673791 (32.4% less interest for that interval).

**Why:** risk-free excess returns and Sharpe/Sortino can be misstated. The main source is precise and fixable, independent of a more advanced statistical method.

**Required correction:** explicitly normalize the index to nanoseconds **before** reading integer epochs: `np.diff(equity.index.as_unit("ns").asi8) / 86_400_000_000_000`. `.asi8` counts the units of the index's *stored resolution*, which can be microseconds in Pandas 3.x and can also be seconds or milliseconds. Without `.as_unit("ns")`, division by nanoseconds-per-day can underestimate elapsed days by **1,000×** for a microsecond-backed index. `.asi8` is not intrinsically a nanosecond array; deprecation claims are not necessary to establish this bug. An alternative is `(equity.index[1:] - equity.index[:-1]).total_seconds() / 86_400.0`. Require an explicit timezone and a strictly increasing `DatetimeIndex` for economically time-based accrual; test DST/weekend/rate changes and `datetime64[s]`, `[ms]`, `[us]`, `[ns]` input resolutions. The independently verified March 2026 example remains **71 hours**, 2.958333 days, effective 5% APR accrual 0.000395523593 vs 0.000267379105 under the existing truncation, or ~32.3987% understated interest for that interval.

### FP-06 — BLOCKING (P1): research trial registry is not transactional with evaluation

**Evidence:** `src/lab/experiments.py`, `_compare()` around lines 283–309 runs candidate backtests before calling `register_trial()`, which writes its trial fingerprint and returns in separate steps. `src/lab/research_state.py` does record a general period exposure **before** evaluation, which is good, but it does not provide equivalent durable per-candidate trial identity. A process crash after observation of candidate performance, but before `register_trial`, can omit an evaluated configuration from the DSR trial ledger.

**Required correction:** reserve each strategy+parameters+data+runtime+policy trial durably **before** calling the backtester; transition to completed/failed using append-only, idempotent evidence. Conservatively count started/failed/evaluated attempts for selection exposure; ensure restart cannot erase tested variants. Test crash injection at preflight, immediately after result computation, and between trial payload and ledger persistence.

### FP-07 — POLICY GAP (P1): diagnostic built-ins versus strategy-qualified autonomous operation

**Evidence:** `src/lab/paper.py`, `build_node()`, requires artifact approval only when `spec.path is not None` (generated signal source). Built-in signal specs can reach the US-equity paper node without passing the same research-promotion proof.

**Why:** *safe to execute in a paper account* is not equivalent to *has a statistically supported economic advantage*. This is acceptable in an explicitly marked experimentation mode, not as an institutional autonomous default.

**Required correction:** separate `diagnostic_paper` from `qualified_autonomous_demo` in authority and UI. Before the latter, require a digest-bound strategy-plus-portfolio policy, completed null/holdout/cost qualification, explicit operator limits, and a current evidence status for built-ins as well as generated signals. Permit deliberate manual paper experimentation without falsely declaring it an approved investment strategy.

## 3. Separately acknowledged incomplete work — do not mislabel as proven defects

- **Crypto market data:** hash-pinned local books and receipt-order `QuoteTick` replay establish local consistency, not authenticated source, complete history, independent mark/index/funding and exchange availability. Native L2 partial fills currently use a controlled synthetic fixture.
- **Crypto funding:** Decimal signed funding oracle verifies a simulated receipt, not authenticated exchange settlement, financing cashflows, margin tiers or liquidation parity.
- **Cross-market OMS:** US equities paper architecture exists, but the crypto offline simulator is still not the single qualified Nautilus strategy/risk/execution flow. Demo broker gateway, authenticated reconciliation and real-money authorization remain blocked.
- **Portfolio risk and convex worst cases:** `src/lab/reservations.py:check_openings()` already uses **O(N)** min/max interval bounds of `(a*lo, a*hi, b*lo, b*hi)` per stock for independently partially filled **one-direction** target transitions and independently bounded positive marks. For separable linear holdings and convex, piecewise-linear absolute exposure/constant-rate margin constraints at those fixed bounds, the worst violation over a rectangular independent fill region is attained at a vertex; no 50%-grid sampling is necessary. The per-coordinate interval bounds and sums are conservative for the stated box assumptions; this is **not** a universal proof for correlated prices/equity, changing risk schedules, overlapping same-instrument orders, dynamic execution paths or liquidation. 

  The distinct crypto `src/lab/markets/portfolio_risk.py:preflight_pending()` currently checks `{0, 0.5, 1}^k` discrete fill scenarios (up to the configured bound) with signed-position transitions and directional price-gap/fee/funding assumptions. **Do not inherit the equities proof automatically.** Refactor its risk checks using formally specified convexity/monotonicity proofs and validated vertex/interval bounds where applicable. If rounding, step/tier schedules, per-order minimum fees, reduce-only state changes, gap paths, collateral haircuts or liquidation make the objective non-convex/discontinuous, enumerate analytically relevant boundaries or construct a conservative envelope; a single midpoint does **not** establish safety. Until then keep D1/D2 risk qualification partial.
- **Statistical qualification:** current holdout, DSR/PBO/bootstrap machinery is meaningful and more rigorous than naive backtests, but independent false-positive calibration, power, trial-selection exposure and prospective evidence are release blockers, not solved by more impressive model names.
- **Operational acceptance:** Windows/Arch install, real gateway recovery, storage-power-loss behavior, data-source failures, broker changes, and supervised multi-session paper/demo acceptance require separate evidence. A green unit-test suite cannot substitute for them.

## 4. Foundations currently implemented well (not certified end-to-end)

- A paper-only US equity guard, kill/halt controls and broker reconciliation checks already exist.
- Exact Decimal accounting for linear derivative position transitions, price/quantity validation, positive/negative funding cashflows, and synthetic native fill parity provide a useful independent oracle.
- The reference simulator refuses external execution modes; explicit D3/D6 gates remain blocked/disabled.
- A persistent research-exposure record and one-use holdout mechanism exist; they should be extended, not replaced, with per-trial transactional reservations.
- Deterministic replay, duplicate-identity protection, append-only local journal controls and source-time versus receipt-time separation demonstrate sound directions, while their qualification boundaries remain explicit.

## 5. Required acceptance before resuming installation/testing as a release candidate

**A — Economic invariants:** exact signed holdings, cash and realized/unrealized PnL; no invented venue trades; zero-value or delayed settlement; no look-ahead from data timing; a valid no-trade result.

**B — Configuration parity:** separate unconstrained scientific *signal screening* from **deployment-bound validation**, and ensure the approved deployment policy, capital, costs, limits, source-quality checks and operator authority match the paper account or fail closed. Named-profile caps already carry through scientific runs; custom-profile screening is intentionally relaxed but cannot itself authorize the configured paper policy.

**C — Scientific invariants:** each attempted candidate durably accounted for before evaluation; holdout not reused silently; all costs, benchmark and multiple testing charged; no promotion for unqualified performance or missing source coverage.

**D — Execution and recovery:** broker acceptance and partial fills distinct from intent; unknown states freeze additions; every cashflow reconciles; no phantom liquidity; dead-letter, cancel/fill race, restart and hard halt survive faults.

**E — Independent proof:** targeted edge/property tests; full pinned Python CI, lint, packaging, example, Docker, multi-session harness; Claude review of numbered findings under collaboration protocol; supervised paper/demo acceptance with **no live money**.

## 6. Recommended sequencing

1. Prioritize **FP-01, FP-02, FP-03, FP-04 and FP-07** before promising institutional trading behavior. FP-02 specifically requires a **deployment-bound requalification path** without deleting the intentional scientific control. FP-05 is a bounded high-confidence arithmetic correction with mandatory unit normalization. FP-06 makes the research selection claims auditable.
2. Create a single `TradeDecision` evidence contract: instrument/venue; as-of and receipt times; validated hypothesis/policy version; expected benefit range; estimated fee/funding/slippage/capacity; downside stress; binding risk limits; order plan; and an explicit `ABSTAIN` outcome.
3. Require the same decision contract and state-machine transitions in backtest and supervised paper/demo; preserve the existing Nautilus agent stack, with appropriately qualified account adapters.
4. Only after those invariants and tests hold, qualify stronger optimizers, market microstructure models, statistical estimators and any ML model individually against the simple baseline **net of all costs**.

**Change and CI note:** This report is evidence and recommendations, not a claim of source-code fixes or passing repository CI. The original focused review was posted in GitHub collaboration thread `G017` on `chatgpt/first-principles-review-20261026` (commit `6e522388`). This R2 document corrects the review record; follow-up implementation and acceptance require separate changes and tests. The original attempt to modify `src/lab/metrics.py` was blocked and did not land.