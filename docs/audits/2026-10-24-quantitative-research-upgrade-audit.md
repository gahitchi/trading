# Quantitative Research & Architecture Upgrade Audit — 24 October 2026

**Repository:** `gahitchi/trading` (`trading-lab` v0.2.0)  
**Objective:** Elevate `trading-lab` from a hardened paper-trading prototype into an institutional-grade quantitative research platform.  
**Scope:** Read-only architectural, econometric, microstructural, and systems audit of the repository state across `src/lab/`, `tests/`, `tools/`, and `docs/`.

---

## Executive Assessment

`trading-lab` is architected significantly better than typical open-source backtesting frameworks. Several foundational engineering choices are already at a high standard and should be preserved unchanged:

1. **Unified Event-Driven Agent Topology:** Running identical `SignalAgent`, `CrossSectionalAgent`, `AllocatorAgent`, `RiskAgent`, `ExecutionAgent`, and `RecorderAgent` actors over `nautilus_trader==2.0.0rc7` in both `BacktestEngine` and `LiveNode` eliminates dual-codebase translation bugs.
2. **OS-Level Sandbox Isolation for AI Signals:** Executing LLM-generated strategy code in a separate process (`src/lab/research/isolation.py`) locked down prior to compilation via a default-deny Linux `libseccomp` filter (`src/lab/research/syscalls.py`), descriptor closure, and `RLIMIT` caps is a major security strength.
3. **Transactional Exposure & Holdout Protection:** Committing research and holdout exposure attempts to `results/research.sqlite3` (`PRAGMA synchronous=FULL`, `journal_mode=WAL`) *before* strategy code sees data (`src/lab/research_state.py`) prevents silent holdout snooping and retry p-hacking.
4. **Cryptographic Artifact & Context Binding:** Binding source SHA-256 digests, parameters, dataset digests, and deployment risk/cost contexts across `train.json`, `test.json`, and `approval.json` (`src/lab/research/approval.py`) ensures promoted strategies match what was validated.

However, because recent engineering cycles focused heavily on **live paper-broker fault recovery, state persistence, and operator authority**, several **statistical, econometric, microstructural, and portfolio-construction approximations** remain in the research path. To reach an **extremely professional trading tool for research purposes**, the upgrades below should be implemented across five core pillars.

---

## Pillar 1: Statistical & Econometric Rigor

### F-R01 — Autocorrelation-Consistent (HAC) Variance in PSR & DSR
* **Location:** `src/lab/stats.py:36-42` (`psr`)
* **Current Behavior:**
  `psr()` estimates the variance of the sample Sharpe ratio using the Bailey & López de Prado (2012) non-normal estimator:
  ```python
  var = 1 - skew * sr + (kurt - 1) / 4 * sr**2
  return _N.cdf((sr - sr_benchmark) * math.sqrt(t - 1) / math.sqrt(var))
  ```
* **The Flaw:** This formula adjusts for skewness ($\hat{\gamma}_3$) and kurtosis ($\hat{\gamma}_4$) under the assumption that returns $r_1, \dots, r_T$ are **independently and identically distributed (i.i.d.)**. In practice, volatility-targeted portfolios (`AllocatorAgent._vol_scale`), trend-following strategies (`sma_trend`), and mean-reversion signals (`rsi_reversion`, `vwap_reversion`) exhibit non-trivial serial correlation. Positive autocorrelation causes $\sqrt{T - 1}$ to overstate the effective sample size, yielding artificially narrow standard errors and inflated PSR/DSR values.
* **Professional Upgrade:**
  Replace the i.i.d. asymptotic variance with a **Newey-West (1987) Heteroskedasticity and Autocorrelation Consistent (HAC)** estimator on the joint moment vector $g_t = (\hat{\mu}_t, \hat{\sigma}^2_t)$ via the Delta Method (Lo, 2002; Mertens, 2002), using automatic Bartlett kernel bandwidth $L = \lfloor 4 (T / 100)^{2/9} \rfloor$:
  $$\hat{V}_{\text{HAC}}(\widehat{SR}) = \frac{1}{T} \nabla f(\hat{\theta})^\top \left( \hat{\Gamma}_0 + \sum_{\ell=1}^{L} \left(1 - \frac{\ell}{L+1}\right) (\hat{\Gamma}_\ell + \hat{\Gamma}_\ell^\top) \right) \nabla f(\hat{\theta})$$
  At minimum, adjust the effective sample size $T_{\text{eff}}$ for first-order and higher-order return autocorrelation $\hat{\rho}_k$:
  $$T_{\text{eff}} = \frac{T}{1 + 2 \sum_{k=1}^{L} \left(1 - \frac{k}{L+1}\right) \hat{\rho}_k}$$

---

### F-R02 — Effective Number of Independent Trials ($\hat{N}$) & Robust Variance in DSR
* **Location:** `src/lab/stats.py:45-68` (`expected_max_sharpe`, `dsr`), `src/lab/experiments.py:130-165` (`register_trial`, `trial_sharpes`)
* **Current Behavior:**
  `dsr()` sets `n = max(1, len(trial_sharpes))` and `var = float(np.var(trial_sharpes, ddof=1))` across every fingerprint recorded in `results/trials.jsonl`.
* **The Flaw:**
  1. **Zero Penalty on First Trial (`n == 1`):** When `n < 2`, `expected_max_sharpe()` returns `0.0`, making `dsr` identical to unadjusted `psr`.
  2. **Over-Penalizing Correlated Parameter Sweeps:** In parameter grids (e.g., `sma_trend` with `fast=48, 50, 52`) or minor LLM revisions, return streams can have correlation $\rho > 0.95$. Treating $N$ highly correlated runs as $N$ independent draws overstates the multiple-testing hurdle $SR_0$.
  3. **Outlier Contamination in `np.var(trial_sharpes)`:** If the AI swarm or a researcher evaluates one degenerate hypothesis that produces an extreme negative Sharpe (e.g., $\widehat{SR}_{\text{annual}} = -4.0$), the sample variance `np.var(trial_sharpes)` explodes. Because $SR_0 \propto \sqrt{\text{Var}(\widehat{SR})}$, a single terrible trial paradoxically raises the expected maximum Sharpe of unskilled strategies, making it nearly impossible for subsequent genuine strategies to pass `min_dsr = 0.95`.
* **Professional Upgrade:**
  1. Store a compact return-signature vector (or daily active return series hash/array) per trial and compute the **Effective Number of Independent Trials ($\hat{N}$)** following López de Prado (2019):
     $$\hat{N} = 1 + (N - 1) (1 - \bar{\rho})$$
     where $\bar{\rho}$ is the average off-diagonal pairwise correlation across the $N$ tried return streams (or derived from the participation ratio of the correlation matrix eigenvalues).
  2. Replace raw `np.var(trial_sharpes, ddof=1)` with a **robust scale estimator**—specifically the **Median Absolute Deviation (MAD)** scaled for normality ($\hat{\sigma}_{SR} = 1.4826 \cdot \text{median}(|\widehat{SR}_i - \text{median}(\widehat{SR})|)$) or Winsorized variance—so broken/degenerate trials cannot corrupt the DSR hurdle.

---

### F-R03 — Combinatorial Purged Cross-Validation (CPCV) in `pbo()`
* **Location:** `src/lab/stats.py:86-129` (`pbo`)
* **Current Behavior:**
  `pbo()` partitions the $(T \times N)$ return matrix into `splits` contiguous blocks via `np.array_split(np.arange(t), splits)` and concatenates in-sample (`is_idx`) and out-of-sample (`oos_idx`) blocks directly.
* **The Flaw:**
  Standard CSCV without **purging** and **embargoing** suffers from boundary leakage whenever strategies maintain state (`self.last`) or use rolling lookbacks (`lookback` up to 250 bars). At every boundary where an OOS block immediately follows an IS block, the signal's position and rolling indicators during the first `lookback` bars of the OOS block are direct functions of IS prices.
* **Professional Upgrade:**
  Upgrade `pbo()` to **Combinatorial Purged Cross-Validation (CPCV)** (López de Prado, 2018):
  * Accept `purge_bars: int` (defaulting to the maximum `lookback` of the evaluated grid) and `embargo_pct: float = 0.01`.
  * Whenever an IS block immediately follows an OOS block (or vice versa), drop `purge_bars` observations at the boundary and apply an embargo of $\lceil T \cdot \text{embargo\_pct} \rceil$ bars to eliminate serial-correlation bleed across train/test splits.

---

### F-R04 — Stationary Bootstrap & Null-of-the-Null Calibration
* **Location:** `src/lab/stats.py:71-83` (`sharpe_ci`), `src/lab/data.py` (`block_bootstrap_panel`)
* **Current Behavior:**
  `sharpe_ci()` uses a fixed block size (`block = 20`) with circular wrap-around (`% n`), and `block_bootstrap_panel()` uses a fixed block length with a provisional 25% fallback threshold when late-listed assets lack donor history.
* **The Flaw:**
  1. Fixed block lengths create periodic artifacts at multiples of `block`, and circular wrapping (`% n`) artificially stitches the final date of the series (e.g., 2022 bear market) onto the first date (e.g., 2010 recovery).
  2. Percentile bootstrap intervals on the Sharpe ratio have poor finite-sample coverage under skewed, heavy-tailed returns.
* **Professional Upgrade:**
  1. Replace the fixed circular block bootstrap with the **Politis & Romano (1994) Stationary Bootstrap**, where block lengths are drawn from a geometric distribution $\text{Geom}(1/b)$ with automatic optimal mean block length selection (Patton, Politis & White, 2009).
  2. Compute **Studentized bootstrap confidence intervals** (dividing each bootstrap resample's centered Sharpe by its own HAC standard error) rather than raw percentile quantiles.
  3. Complete the multi-asset **Null-of-the-Null calibration** across staggered listings ($0\%$, $10\%$, $25\%$, $40\%$ fallback rates) with $M \ge 500$ outer Monte Carlo paths and $B \ge 199$ inner resamples so the empirical Type-I error rate at $\alpha = 0.05$ is verified within a tight binomial confidence interval.

---

### F-R05 — Excess-Return Ratios, Risk-Free Rate ($r_{f,t}$) & Session-End Resampling
* **Location:** `src/lab/metrics.py:10, 22-82` (`summarize`)
* **Current Behavior:**
  `summarize()` computes `rets = equity.pct_change().dropna()` and scales `mean` and `vol` by `TRADING_DAYS = 252` assuming a $0\%$ risk-free rate ($r_f = 0$).
* **The Flaw:**
  1. In historical windows spanning both zero-interest-rate regimes (2010–2021) and $5\%+$ T-bill regimes (2005–2007, 2023–2026), setting $r_f = 0$ distorts Sharpe, Sortino, and PSR across eras.
  2. Furthermore, uninvested cash earns $0\%$ in backtests, systematically penalizing low-exposure risk profiles (`Calm` at $0.30\times$ max gross, `Cautious` at $0.50\times$ max gross) and strategies that go to cash during high-volatility regimes.
  3. If `summarize()` is ever passed an equity series with multiple marks per session (or missing holiday sessions), `rets.mean() * 252` mis-annualizes the return and volatility.
* **Professional Upgrade:**
  * Explicitly resample `equity` to **1 mark per exchange session close** inside `summarize()` before calling `.pct_change()`.
  * Support an optional point-in-time daily risk-free rate series $r_{f,t}$ (e.g., 3-month T-bill / SOFR daily accrual) so excess returns $r^e_t = r_t - r_{f,t}$ are used in Sharpe/Sortino/PSR and uninvested cash accrues the risk-free yield (minus a configurable broker spread).

---

## Pillar 2: Market Microstructure, Costs & Financing Realism

### F-R06 — Square-Root Market Impact & Volume Participation Caps
* **Location:** `src/lab/costs.py:39-42` (`fill_model`), `src/lab/universe.py:67-83, 115-135` (`to_market_events`, `to_intraday_events`)
* **Current Behavior:**
  `fill_model()` uses `DefaultFillModel(prob_fill_on_limit=1.0, prob_slippage=1.0 if c.slippage else 0.0, random_seed=42)`, which applies a flat 1-tick ($\text{\$0.01}$) slippage to market fills regardless of price level, volatility, or order size. Opening prints are generated with volume `int(v[i] * open_print_fraction)`, but market orders do not enforce a partial-fill cap against that print size.
* **The Flaw:**
  1. A flat $\text{\$0.01}$ slip is $0.2\text{ bps}$ on a $\text{\$500}$ stock (unrealistically cheap) and $20\text{ bps}$ on a $\text{\$5}$ stock.
  2. Strategies can trade $\text{\$10M}$ notional or $100\%$ of an illiquid stock's opening print with zero price impact, hiding capacity constraints and favoring high-turnover small/mid-cap signals.
* **Professional Upgrade:**
  Implement a parameterized **Microstructural Slippage & Impact Model** in `src/lab/costs.py`:
  $$\text{Slippage}_{i,t}(\text{bps}) = \underbrace{\frac{\hat{s}_{i,t}}{2}}_{\text{Half-Spread}} + \underbrace{\eta \cdot \sigma_{i,t} \cdot \sqrt{\frac{|Q_{i,t}|}{\text{ADV}_{i,t}}}}_{\text{Square-Root Temporary Impact}}$$
  where:
  * $\hat{s}_{i,t}$ is the **Corwin & Schultz (2012) High-Low spread estimator** (computed purely from daily OHLC bars with zero extra data dependencies, or a configurable minimum bps floor).
  * $\sigma_{i,t}$ is the stock's trailing 20-day daily volatility, $\text{ADV}_{i,t}$ is trailing 20-day average daily volume, and $\eta \in [0.1, 0.5]$ is the market-impact coefficient.
  * Enforce a hard **volume participation cap** (e.g., `max_volume_participation = 0.05` of bar/auction volume) so oversized orders either partially fill or are clipped by the execution agent.

---

### F-R07 — Split-Adjusted Price Quantization & Historical Commission Distortion
* **Location:** `src/lab/universe.py:39, 59-62, 115`
* **Current Behavior:**
  `equity()` defines all US stocks with 2 decimal places (`Price.from_str("0.01")`), and `to_market_events()` rounds all OHLC series via `df["open"].round(2)`. Commissions are calculated per share (`$0.005/share` in `ibkr_fixed_commission`).
* **The Flaw:**
  When `data.fetch_yfinance(..., auto_adjust=True)` imports split- and dividend-adjusted histories for stocks that underwent major forward splits (e.g., AAPL $56:1$ cumulative split, NVDA $40:1$ split), historical back-adjusted prices in 2010–2014 are $\text{\$0.40}$ to $\text{\$4.00}$ instead of their actual unadjusted trading prices ($\text{\$100}$ to $\text{\$600}$).
  1. Rounding a $\text{\$0.50}$ back-adjusted price to $\text{\$0.01}$ ticks introduces $2\%$ discrete price quantization steps.
  2. Sizing $\text{\$25,000}$ at a $\text{\$0.50}$ adjusted price buys $50,000$ adjusted shares, charging $\text{\$250.00}$ in IBKR per-share commissions ($1.0\%$ cap hit on every single trade!) when the actual unadjusted trade of $40$ shares at $\text{\$600}$ would have cost $\text{\$1.00}$ ($0.4\text{ bps}$).
* **Professional Upgrade:**
  * **Option A (Gold Standard):** Store **unadjusted OHLCV** alongside a split/dividend factor column (`split_factor`, `div_amount`) so order sizing, tick rounding, and per-share commissions occur at true historical trade prices while signals see split-adjusted continuous series.
  * **Option B (Lightweight Fix for Adjusted Series):** When `manifest[ticker]["adjusted"] == True`, configure `SIM` instruments with `precision=4` (`tick_size="0.0001"`) and evaluate per-share commissions using a normalized reference price ($\max(P_{i,t}, \text{\$50.00})$) or pure basis-point fee schedule so pre-split decades are not penalized by $40\times$ inflated per-share commissions.

---

### F-R08 — Financing Symmetry: Short Collateral Rebate & Cash Sweep Yield
* **Location:** `src/lab/margin.py:33-53` (`borrowed_cash`, `financing_cost`)
* **Current Behavior:**
  `financing_cost()` charges `borrow_apr` on `short_value` and `debit_apr` on `borrowed_cash(cash, short_value) = max(0.0, short_value - cash)`.
* **The Flaw:**
  In a real prime-brokerage or IBKR Pro account, short sale proceeds held as collateral earn a **short credit interest rebate** (benchmark rate minus a haircut) on easy-to-borrow names, or `borrow_apr` is quoted net of the rebate. Furthermore, positive free cash (`cash - short_value > 0`) earns credit interest above a threshold. Omitting short rebates and cash interest creates an artificial drag on dollar-neutral long/short portfolios (`momentum_ls`).
* **Professional Upgrade:**
  Extend `MarginSettings` with `credit_apr: float = 0.0` (interest earned on positive free cash `max(0.0, cash - short_value)`) and `short_rebate_apr: float = 0.0` (or clarify in `BorrowBook` whether vendor APRs are gross fee rates or net of rebate).

---

### F-R09 — Point-in-Time Corporate Actions, Short Dividend Debits & Delisting Recovery
* **Location:** `src/lab/backtest.py:55-72` (`active_windows`), `src/lab/data.py`
* **Current Behavior:**
  `active_windows()` refuses any backtest where a stock's price series ends more than 5 days before the universe end unless `membership.csv` declares an exit at least two bars prior (`exit_limit = df.index[-3].date()`).
* **The Flaw:**
  While this properly prevents look-ahead liquidation on the final bar (fixing G007-F17 / G009-F24), real survivorship-free datasets contain delistings (bankruptcies, distress delistings, cash mergers, exchange transfers) where membership ends *on* the final trading day and no subsequent bars exist. Refusing the run blocks research on true survivorship-free panels unless the user manually backdates `membership.csv` by 2 days (which reintroduces 2-day prescient exit look-ahead).
* **Professional Upgrade:**
  Add support for `data/delistings.csv` (`ticker,delist_date,delist_type,delist_return,settlement_price,source`):
  * When a held position reaches `delist_date`, the simulator settles the position at `close * (1 + delist_return)` (or `settlement_price`) without requiring two subsequent bars of normal trading.
  * Require explicit vendor provenance for every delisting row, and flag any run using a fallback assumption (e.g., Shumway $-30\%$ distress default) in `data_quality`.

---

## Pillar 3: Portfolio Construction, Sizing & Intraday Mechanics

### F-R10 — Ledoit-Wolf Shrinkage & Chaining-Free HRP / HERC
* **Location:** `src/lab/allocation.py:39-84` (`_single_linkage_tree`, `_cluster_var`, `hrp`)
* **Current Behavior:**
  `hrp()` computes the raw sample covariance matrix `cov = np.atleast_2d(np.cov(rets))` (requiring as few as `MIN_RISK_RETURNS = 20` shared returns), constructs a **single-linkage** tree in an $O(N^3)$ Python loop, and splits capital top-down at each internal node (`left, right = _leaves(node[0]), _leaves(node[1])`) in proportion to inverse cluster variance:
  ```python
  v_left, v_right = _cluster_var(cov, left), _cluster_var(cov, right)
  alpha = 1.0 - v_left / (v_left + v_right)
  w[left] *= alpha
  w[right] *= 1.0 - alpha
  ```
* **The Flaw:**
  1. **Unshrunk Sample Covariance at $T=20$:** With $N \ge 10$ stocks and $T = 20$ returns, the sample covariance and correlation matrices are noisy and ill-conditioned.
  2. **Single-Linkage Chaining + Asymmetric Tree Splits:** Single linkage is notorious for **chaining**—peeling off one moderately uncorrelated stock at the root of the dendrogram against a giant cluster of $N - 1$ stocks (`left = [0]`, `right = [1, 2, ..., N-1]`). Because `_cluster_var(cov, right)` computes the variance of a diversified $N-1$ stock portfolio while `_cluster_var(cov, left)` is the variance of a single stock, `v_right` is much smaller than `v_left`, giving the $N-1$ group e.g. $70\%$ of the budget and the **single outlier stock $30\%$ of the entire portfolio** (whereas equal weight is $1/N$). Conversely, if the single outlier happens to have slightly lower volatility than the cluster, it can capture $>50\%$ of total portfolio capital at depth 1!
* **Professional Upgrade:**
  1. **Ledoit-Wolf Shrinkage:** Apply analytical Ledoit-Wolf shrinkage toward the constant-correlation or diagonal target before computing correlation distances and cluster variances (implementable in ~15 lines of pure NumPy with zero external dependencies).
  2. **Fix the Dendrogram Split / Linkage:**
     * Either use **Ward linkage** / **Average (UPGMA) linkage** when splitting along dendrogram branches (Hierarchical Equal Risk Contribution — Raffinot, 2018), **or**
     * Follow López de Prado's (2016) **Quasi-Diagonalization (Seriation) + Recursive Bisection** (`list[:len//2]`, `list[len//2:]`), where leaf order is determined by the dendrogram so correlated twins stay adjacent, or scale cluster allocations by cluster size / risk contribution so a 1-asset branch at the root cannot absorb half the portfolio's capital.

---

### F-R11 — Pairwise-Overlapping Covariance vs. All-Asset Intersection
* **Location:** `src/lab/allocation.py:24-31` (`matched_returns`), `src/lab/agents/allocator.py:113-131` (`_vol_scale`)
* **Current Behavior:**
  Both `matched_returns()` and `AllocatorAgent._vol_scale()` intersect return timestamp intervals across **every** stock simultaneously (`shared = sorted(set.intersection(*(set(r) for r in per.values())))`).
* **The Flaw:**
  In a 20-stock universe (especially at intraday speeds `5m`/`15m` or with staggered histories), if a single stock misses scattered bars or entered the universe 15 sessions ago, `len(shared)` drops below `20`. This causes `risk_weights()` to abruptly snap the entire 20-stock portfolio back to `1/N` equal weights and causes `_vol_scale()` to return `0.0` (forcing the entire portfolio into $100\%$ cash!).
* **Professional Upgrade:**
  Compute pairwise covariance entries $\hat{\Sigma}_{ij}$ over the timestamps shared by pair $(i, j)$ (requiring at least `min_obs` shared intervals per pair), fall back to diagonal variance for any newcomer lacking pairwise history, and project $\hat{\Sigma}$ onto the nearest Positive Semi-Definite (PSD) matrix via eigenvalue clipping ($\lambda_k \leftarrow \max(\lambda_k, \epsilon)$).

---

### F-R12 — Overnight Gap vs. Intrabar Variance in Intraday Volatility Targeting
* **Location:** `src/lab/agents/allocator.py:113-131` (`_vol_scale`), `src/lab/backtest.py:118`
* **Current Behavior:**
  At quick speeds (`1h`, `15m`, `5m`), `backtest.py` passes `periods_per_year = 252 * sp.bars_per_session` to `AllocatorAgent`. `_vol_scale()` computes `pairwise(rows)` across all consecutive closes—including the overnight jump from Day $t$ `16:00` to Day $t+1$ first bar—and multiplies the sample variance of those returns by `252 * sp.bars_per_session`.
* **The Flaw:**
  At `5m` speed (`bars_per_session = 78`), the overnight return (spanning 17.5 hours of earnings and macro news) is treated as one of 78 intraday 5-minute bars and its squared return is effectively multiplied by $252 \times 78 = 19,656$ when averaged into `np.cov(rets)`. Furthermore, when `flat_at_close = True`, the strategy **never holds overnight**, so including overnight gaps in its ex-ante volatility estimate systematically under-allocates capital after any overnight market move.
* **Professional Upgrade:**
  In `AllocatorAgent._vol_scale()`, inspect whether both endpoints $(a_0, b_0)$ of a return interval belong to the same New York session date:
  * When `flat_at_close = True`, exclude cross-session overnight pairs from `returns[t]` so ex-ante volatility reflects purely the intrabar risk actually held.
  * When `flat_at_close = False` (holding overnight at quick speeds), estimate intrabar covariance $\hat{\Sigma}_{\text{intra}}$ and overnight covariance $\hat{\Sigma}_{\text{overnight}}$ separately:
    $$\hat{\Sigma}_{\text{annual}} = 252 \cdot \left( B \cdot \hat{\Sigma}_{\text{intra}} + \hat{\Sigma}_{\text{overnight}} \right)$$

---

### F-R13 — Reservation Split & Rebalance-Band Cap-Breach Override
* **Location:** `src/lab/reservations.py:52-85` (`stressed_openings`), `src/lab/agents/execution.py`
* **Current Behavior:**
  On `main`, `stressed_openings()` applies simultaneous $3\%$ adverse price shocks and deducted gap reserves to the denominators of policy caps (`max_weight`, `max_gross`, `max_short_gross`, `min_net`, `max_net`). On the unmerged branch `claude/reservation-split` (discussed in thread `G013`), `split_openings()` evaluates policy caps at decision marks net of entry slippage/fees and applies the $3\%$ gap stress only to cash and initial margin—but exposed a subtle edge case where a levered book (`1.487x` against a `1.50x` cap) eroded by debit interest drifts to `1.524x` without being trimmed because the $2.4\%$ drift is within `rebalance_band` per stock.
* **The Flaw:**
  1. Keeping `stressed_openings()` on `main` starves unlevered portfolios (`buy_and_hold` on `Balanced` averages $96\%$ exposure instead of $99\%$ and requires 3 top-up fills) and inflates turnover on active strategies (`rsi_reversion` fills jump from 69 to 106).
  2. Allowing `rebalance_band` to skip reductions when the held book is currently over an exposure ceiling permits passive drift breaches.
* **Professional Upgrade:**
  Integrate `claude/reservation-split` (`split_openings`) into `main` **together with Rule (a) from G013**: in `ExecutionAgent`, bypass `rebalance_band` for any reduction order whenever the current mark-to-market book (or post-financing book) exceeds `max_weight`, `max_gross`, `max_short_gross`, or `[min_net, max_net]`.

---

### F-R14 — Active-Window Evaluation in `walk_forward.py`
* **Location:** `src/lab/signals/walk_forward.py:21-29, 43-51` (`ScheduledSignal.target`, `ScheduledCrossSectional.weights`)
* **Current Behavior:**
  On every single bar of a continuous walk-forward backtest, `ScheduledSignal.target()` loops over **every** `(day, sig)` pair in `self.schedule` and calls `sig.target(tail(w, sig.lookback))`, keeping the last one where `w.dates[-1] >= day`.
* **The Flaw:**
  If a walk-forward study has 15 rolling windows and evaluates an isolated research signal (`IsolatedSignal`), every bar dispatches 15 IPC pipe calls across 15 worker processes, 14 of which are for expired past windows or future windows not yet active.
* **Professional Upgrade:**
  Find the active schedule index $k = \max \{ i \mid \text{day}_i \le \text{now} \}$. Call `sig.target()` only for the active signal $k$ (and, if signal $k+1$ is stateful and within `sig.lookback` bars of its activation date $\text{day}_{k+1}$, warm up signal $k+1$). Close worker processes for expired schedule entries $i < k$.

---

## Pillar 4: AI Research Swarm & Factor Diagnostics

### F-R15 — Stage-0 Factor Tearsheet (IC, ICIR, Quantile Spreads & Turnover Decay)
* **Location:** `src/lab/research/worker.py:42-77`, `src/lab/research/roles.py`, `src/lab/research/orchestrator.py:92-120`
* **Current Behavior:**
  `worker.py` evaluates candidate signals exclusively by running an end-to-end portfolio backtest (`experiments.compare`) and recording portfolio-level metrics (`sharpe_edge`, `p_value`, `dsr`, `fills`). `Orchestrator.past_for_researcher()` feeds only `train_sharpe_edge_vs_buy_and_hold` and `noise_floor_p_value` back to the AI Researcher.
* **The Flaw:**
  A full backtest conflates **raw signal predictive power** with **portfolio construction, rebalance bands, and transaction cost drag**. If a signal has genuine predictive power at a 5-day horizon ($\text{Rank IC} = +0.04$, $t = 3.2$) but flips direction too frequently on daily noise, its net-of-cost portfolio Sharpe will be negative. Seeing only `sharpe_edge = -0.15`, the Researcher discards the underlying economic mechanism rather than smoothing the signal's turnover.
* **Professional Upgrade:**
  Add a fast, vectorized **Stage-0 Signal Diagnostics (`lab/factor_stats.py`)** step inside `worker.py` prior to (or alongside) `experiments.compare`, and include its summary in `train.json` and `past_for_researcher()`:
  1. **Information Coefficient (IC) Profile:** Spearman Rank IC and Pearson IC at forward horizons $h \in \{1, 5, 10, 21\}$ bars, along with **ICIR** ($\text{mean}(\text{IC}_h) / \text{std}(\text{IC}_h)$) and Newey-West $t$-statistics.
  2. **Gross vs. Net Return Attribution:** Gross active Sharpe (zero commissions/slippage) vs. Net active Sharpe, isolating exact **cost drag in Sharpe units**.
  3. **Signal Autocorrelation & Holding Half-Life:** Lag-1 autocorrelation $\text{Corr}(s_{i,t}, s_{i,t-1})$ and implied average holding period $\frac{1}{1 - \hat{\rho}_1}$.
  4. **Hit Rate & Payoff Asymmetry:** Win rate on active days and average gain-to-loss ratio.

---

### F-R16 — Scope `source_digest()` to Simulation-Critical Modules
* **Location:** `src/lab/provenance.py:10-16` (`source_digest`), `src/lab/research/approval.py:24` (`deployment_context`)
* **Current Behavior:**
  `source_digest()` hashes every `*.py` file recursively under `src/lab/`:
  ```python
  for p in sorted(root.rglob("*.py")):
      digest.update(str(p.relative_to(root)).encode())
      digest.update(p.read_bytes())
  ```
* **The Flaw:**
  Because `source_digest()` is embedded into `deployment_context()` (for hypothesis validation/promotion) and `register_trial()` (for trial deduplication in `trials.jsonl`), **any cosmetic edit** to `src/lab/app.py`, `src/lab/cli.py`, `src/lab/report.py`, `src/lab/notify.py`, or `src/lab/capture.py`:
  1. Invalidates all previously validated `train.json`, `test.json`, and `approval.json` manifests.
  2. Causes identical backtest runs to register as brand-new trials in `trials.jsonl`, artificially inflating the DSR trial count $N$.
* **Professional Upgrade:**
  Split `provenance.py` into two functions:
  * `runtime_digest()`: hashes all `*.py` files (used for `lab runtime preflight` / `upgrade` on live paper nodes).
  * `simulation_digest()`: hashes **only** the deterministic simulation & accounting modules (`backtest.py`, `costs.py`, `margin.py`, `borrow.py`, `reservations.py`, `allocation.py`, `risk_profiles.py`, `universe.py`, `exchange.py`, `intraday.py`, `data.py`, `signals/base.py`, `signals/builtin.py`, and `agents/{signal_agent,cross_sectional,allocator,risk,execution}.py`). Use `simulation_digest()` in `experiments.py` and `approval.py`.

---

### F-R17 — Regime Hysteresis & Dwell-Time Controls in `StrategyBank`
* **Location:** `src/lab/strategy_bank.py:50-65` (`StrategyBank.select`)
* **Current Behavior:**
  `StrategyBank.select()` classifies the benchmark regime at each bar using single-point step thresholds:
  ```python
  regime = (
      "volatile" if vol >= self.volatility
      else "up" if move > self.trend
      else "down" if move < -self.trend
      else "range"
  )
  ```
* **The Flaw:**
  When trailing benchmark volatility hovers near `self.volatility` (e.g., $19.9\% \leftrightarrow 20.1\%$) or trailing return hovers near `self.trend`, single-day return roll-offs cause **regime chatter**, flipping the active strategy sleeve back and forth on consecutive sessions and incurring full portfolio turnover on each switch.
* **Professional Upgrade:**
  Add an optional **hysteresis deadband** (`volatility_exit = 0.85 * volatility`, `trend_exit = 0.75 * trend`) or a `min_dwell_bars: int = 3` persistence requirement so a regime transition only triggers once the new regime condition holds decisively or persists for $k$ consecutive sessions.

---

## Pillar 5: Engineering Velocity & Pipeline Throughput

### F-R18 — Vectorized Nautilus Event Construction (`BarDataWrangler`)
* **Location:** `src/lab/universe.py:59-83, 115-135` (`to_market_events`, `to_intraday_events`)
* **Current Behavior:**
  Every call to `backtest.run()` (including every single bootstrap resample inside `null_test` and every grid point in `pbo_study`) loops row-by-row in Python (`for i in range(len(df)):`) instantiating `Price`, `Quantity`, `TradeId`, `TradeTick`, and `Bar` objects.
* **The Flaw:**
  In a 10-stock, 2,500-day universe with 200 bootstrap resamples, `to_market_events()` executes $5,000,000$ Python loop iterations and Cython/PyO3 object allocations, dominating runtime CPU.
* **Professional Upgrade:**
  1. Use NautilusTrader's native Cython/Rust `BarDataWrangler` or cache pre-built `Bar` / `TradeTick` lists when `frames` are unchanged across parameter sweeps (`pbo_study`, `walk_forward`).
  2. For `backtest.run()`, allow passing pre-built market events across multiple candidate specs in `experiments._compare()` so the baseline and $K$ candidate strategies share the exact same event conversion pass rather than re-converting `df` $K+1$ times.

---

### F-R19 — Test Suite Tiering (`@pytest.mark.harness` & `pytest-xdist`)
* **Location:** `pyproject.toml:34-41`, `.github/workflows/ci.yml`
* **Current Behavior:**
  Running `pytest` executes all 430+ tests sequentially in ~577 seconds (~9.6 minutes) because real-clock `LiveNode` sleep tests (`tests/test_harness.py`, `tests/test_intraday_harness.py`, `tests/test_control_harness.py`) are mixed with fast unit and research tests.
* **The Flaw:**
  A 10-minute test cycle slows down iterative research development.
* **Professional Upgrade:**
  1. Mark wall-clock `LiveNode` tests with `@pytest.mark.harness` in `pyproject.toml`.
  2. Add `pytest-xdist` to `[project.optional-dependencies] dev` so CPU-bound backtest/statistical tests run in parallel (`pytest -m "not llm and not harness" -n auto`, completing in $<15\text{ s}$ locally), while CI runs both the fast parallel suite and the serialized `harness` suite.

---

## Summary Priority Matrix

| ID | Module | Upgrade | Impact | Effort |
| :--- | :--- | :--- | :--- | :--- |
| **F-R01** | `src/lab/stats.py` | Newey-West (HAC) autocorrelation adjustment in `psr()` / `dsr()` | Critical (Statistical Validity) | Low |
| **F-R02** | `src/lab/stats.py` | Effective Independent Trials ($\hat{N}$) & MAD scale in `dsr()` | Critical (Multiple Testing) | Medium |
| **F-R03** | `src/lab/stats.py` | Purged & Embargoed CPCV in `pbo()` | High (Leakage Prevention) | Low |
| **F-R06** | `src/lab/costs.py` | Corwin-Schultz spread + Square-Root impact + participation cap | Critical (Execution Realism) | Medium |
| **F-R10** | `src/lab/allocation.py` | Ledoit-Wolf covariance shrinkage + chaining-free HRP/HERC | High (Portfolio Stability) | Low |
| **F-R12** | `src/lab/agents/allocator.py` | Separate overnight gap variance from intrabar variance | High (Intraday Sizing) | Low |
| **F-R13** | `src/lab/reservations.py` | Merge `split_openings()` + `rebalance_band` cap-breach override | High (Exposure Accuracy) | Low |
| **F-R15** | `src/lab/research/worker.py` | Stage-0 Factor Tearsheet (Rank IC, ICIR, Decay, Cost Drag) | High (AI Swarm Intelligence) | Medium |
| **F-R16** | `src/lab/provenance.py` | Scope `simulation_digest()` to simulation-critical files | High (Research Ergonomics) | Trivial |
| **F-R05** | `src/lab/metrics.py` | Session-end resampling + risk-free rate ($r_{f,t}$) excess returns | Medium (Multi-Era Accuracy) | Low |
| **F-R07** | `src/lab/universe.py` | 4-decimal adjusted precision + normalized per-share fee basis | Medium (Pre-Split Accuracy) | Low |
| **F-R18** | `src/lab/experiments.py` | Share converted market events across candidate comparisons | Medium (5$\times$ Speedup) | Low |