# Research integrity, reproducibility and model/code isolation

**Owner:** Research Engineering. **Status:** target design, not deployment-ready.
**Invariants:** INV-09, INV-10.

## 1. Data model and acquisition

Minimum immutable ingestion manifest: provider and license, instrument permanent identifier, venue, trading calendar, timezone, bar granularity, currency, original vendor request/response hash, ingestion time, raw file content hash, adjustment methodology and "knowledge-as-of" timestamp.

Required datasets and exclusions:
- Point-in-time universe **memberships** including additions, removals and renamed/delisted names; do not use current index constituents for historical membership.
- Raw (unadjusted) executable price, volume and corporate actions separately; dividends, splits, symbol/venue changes, halts and delisting/recovery returns need independent timestamped inputs.
- Audited adjustment factors with an effective-as-of timeline. Fully backward-adjusted bars can contain information unavailable to a past trader.
- Explicit missing/truncated data quality labels. A price series ending early is **not** an advance notice of delisting or a guaranteed exit fill.
- Risk-free/cash yield, spreads, exchange/venue fees and realistic cash constraints where materially relevant.
- Data must be access-controlled, backed up and immutable for research comparisons. Corrected data generate a **new dataset version**, not a silent overwrite.

Ingestion stages: `FETCHED → STAGED → VALIDATED → QUARANTINED|APPROVED → SEALED`. Checks: duplicates, monotonic timestamps, OHLC relations, positive prices, finite volume, jumps/corporate actions, currency/venue mismatch, missing trading days, split dates, volume spikes and cross-provider conflicts. Approval records both coverage and caveats.

**Current gap:** `yfinance` is an exploration source, not point-in-time institutional historical evidence.

The isolated `market_analysis.py` slice now computes descriptive market
features only, with exact as-of dates and deterministic source hashes.
It does not repair as-of provenance gaps caused by the current storage
pipeline or establish statistically qualified forecasts. See
[market-analysis-engine.md](market-analysis-engine.md).

## 2. Experiment protocol

One immutable experiment record includes:
- pre-registered hypothesis, economic rationale and null hypothesis;
- exact dataset/universe snapshot hashes, validation rules, training/test date boundaries, cost/slippage/fill model, cash/risk parameters;
- strategy source hash, dependency lock hash, code commit, environment fingerprint and random seeds;
- explicit search budget: all parameter combinations, failed attempts and AI-generated variants count as trials;
- chosen statistical test, confidence intervals, effect size and robustness thresholds, **defined before** observing holdout results;
- reviewer verdict, weaknesses and final disposition.

Use chronological rolling/walk-forward splits with a warm-up and purge/embargo where labels overlap. Never treat repeated walk-forward hyperparameter tuning as independent out-of-sample evidence.

Final holdout must be sealed at the **dataset access boundary**, not just one CLI command. Research model, researcher/engineer, ad-hoc scripts, dashboards and logs must not access it until a separately authorized final evaluation. Every unsealing and repeat is recorded and counts against the inference budget.

## 3. Statistical validity

- A null model must preserve the dependencies relevant to the hypothesis: synchronized market shocks, autocorrelation, volatility clustering, cross-asset covariances and regime breaks. Reject incompatible asset calendars rather than independently resampling ticker paths.
- Distinguish **bootstrap null distribution** from bootstrap confidence intervals on observed active returns; a small p-value alone does not prove a useful or economically tradable edge.
- Report performance against a *matched* benchmark with the same initial capital, borrowing/cash assumptions, fees, constraints and rebalancing dates.
- Show drawdown depth and duration, tail risk, turnover, average holdings, concentration, exposures, implementation shortfall, capacity and sensitivity to spread/fees/gaps.
- Apply multiple-trial penalties to the **entire** exploration history (not only the most recent candidate). DSR, PSR, PBO and bootstrap p-values must have explicit assumptions and tests.
- `null_samples=30` is too coarse to justify a stringent 0.05 threshold: attainable empirical p-values are separated by 1/31. Increase sample budget and use deterministic seeds; keep fast smoke tests scientifically distinguished from qualifying analyses.
- Stress on multiple regimes, high/low volatility, data gaps, market closures, COVID-type discontinuities, major splits and untradable securities.
- Any change to data, universe, cost model or strategy source after the final test creates a different experiment and invalidates a previously frozen decision.

## 4. AI researcher trust boundary

An LLM proposal is **untrusted input**, never a privileged operator or production code author:
- Generate source in an isolated worker with no broker credentials, no mounted live secrets, no trading socket, no write access to registry/release paths and no general host access.
- Static AST analysis is defense-in-depth only; require a true OS isolation boundary (locked-down container/VM with restricted network, seccomp, no capabilities, non-root, read-only mounts, resource limits).
- Behavioral checks include deterministic replay, memory/cpu/time caps, data-access contract, numeric output invariants and malicious/escape attempts.
- The Critic is a second reviewer, not proof of safety. Unresolved material objections mean reject. Human code review and signed artifact promotion are mandatory for paper trading.
- Promotion writes an immutable digest of code **and dependencies/config**. On load, runtime recomputes digest; mismatch cannot be silently replaced by code in a mutable research directory.
- The paper process must never dynamically import arbitrary `research/hypotheses/…/signal.py` as a direct effect of a user-editable `status.json`.

## 5. Research acceptance tests

1. Replaying the same experiment manifest twice gives the same inputs, decisions and metrics within documented numeric tolerance.
2. A corrected single data bar changes the dataset hash and prevents accidental result reuse.
3. No code path outside the one-time evaluation runner can open sealed holdout data.
4. All attempts (including invalid or critic-rejected ones) are counted in trial multiplicity.
5. Delisting, index exit and corporate-action timestamps are effective-as-of, not read from future file endings.
6. Cross-asset null tests preserve synchronized shocks and reject unaligned panels.
7. Modified source after promotion fails digest verification.
8. A malicious strategy cannot read credentials, open a broker connection, launch a process or modify the registry.
9. Quantitative reports include cost sensitivity, confidence bounds and test coverage.
10. Paper implementation shortfall is reconciled to the exact originating **order decision**, not just the most recent signal for a ticker.

A profitable backtest is a *hypothesis*, not a deployment safety certificate.
