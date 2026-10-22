# Market intelligence and statistical analysis — technical design

**Status:** first deterministic diagnostics implemented; advanced modeling stages are designed but **not implemented or validated for trading**. No part of this module authorizes an order.

## Scope and technology selection

A professional analysis pipeline should distinguish **measured facts**, **statistical estimates**, **forecasts**, and **trading decisions**. These are different artifacts, each with distinct provenance and evidence requirements.

| Layer | Target technology | Outputs | Current implementation |
| --- | --- | --- | --- |
| Ingestion | Point-in-time vendor snapshots, immutable hashes, exchange calendar, corporate actions, symbol master | Verified raw observations | Partial; current free data not institutional evidence |
| Descriptive features | Versioned causal rolling features, strict data quality, reproducible as-of cutoff | Volatility, market breadth, momentum, liquidity proxy, drawdown and tails | First slice in `market_analysis.py` |
| Dependence and risk | Shrunk PSD covariance, stressed correlations, robust downside/tail diagnostics, scenario replay | Risk diagnostic matrices and uncertainty | Fixed diagonal shrinkage plus historical VaR/ES only |
| Regime inference | Hidden Markov/state-space alternatives, regime uncertainty, drift detectors, disaggregated macro/liquidity inputs | Probabilistic regime *estimates* with confidence and diagnostic plots | **Not implemented** |
| Forecasts | Baseline GLMs / gradient-boosted models, carefully justified deep temporal models/transformers; no automatic model promotion | Forecast distribution with calibration by regime and horizon | **Not implemented** |
| Validation | Walk-forward/purged-and-embargoed evaluation, nested selection, full trial ledger, realistic transaction/borrowing/cash | Reproducible independent evidence | Existing partial statistics and walk-forward; stronger isolation required |
| Portfolio and execution | Factor-risk model, expected shortfall constraints, broker-side available-funds checks, verified fills, deterministic risk permits | Explicit approved allocation and traceable order intents | **Not integrated; paper deployment not cleared** |

**Selection rule:** choose the simplest approach that demonstrably improves *out-of-sample* evidence after costs and satisfies compute budget. Do not assume a transformer, LLM or reinforcement learner is better merely because it is newer. LLMs may generate research hypotheses and critique, but broker order issuance remains deterministic and independently risk-authorized.

## Implemented first analysis slice

`lab analyze --tickers GOOG --as-of YYYY-MM-DD --lookback 252 --json` is a **new read-only CLI workflow**. The supplied date must be an available bar's normalized exchange-session date strictly **before** the configured final holdout. Without `--as-of` the command chooses a conservative pre-holdout cutoff. It does not use a date inside the holdout, even if those rows are already on disk.

`analyze_market`:
- selects historical observations on or before the explicit session *before validation/computation* so future rows cannot affect the result;
- requires all assets to share exactly the same sessions in the selected rolling interval; no forward filling, implicit zero returns or silent dropped rows;
- validates finite positive prices, OHLC consistency and nonnegative volumes;
- estimates 21-session return, 12-1-style momentum (configurable skip-recent), realized volatility, downside deviation, historical 95% loss VaR and expected shortfall, drawdowns, median dollar volume;
- computes **fixed** shrink-to-diagonal annualized sample covariance, with PSD and finite-value checks, null correlation where variance is zero;
- reports positive-market breadth and a simple **descriptive** trend classification: broad uptrend/mixed/broad downtrend. The label is *not* a probability, price forecast or trading signal;
- emits a SHA-256 ID tied to exact selected OHLCV rows, date, feature settings and source manifest, plus clearly flagged missing data/market assumptions;
- marks everything `EXPLORATORY_ONLY`. Qualifying market data requires external point-in-time membership, corporate-action-as-of and source-finality proof.

### Historical risk interpretation

Historical Value-at-Risk and expected shortfall come from the **empirical lookback distribution**. Zero variance, small samples, regime change, discrete jumps, tail censoring, stale data and market illiquidity can make tail estimates misleading. These numbers are neither guaranteed bounds nor forecasts. Median *bar-close × reported volume* is only a turnover proxy; it is not measured spread, slippage, liquidity depth or market impact.

Fixed 0.15 covariance shrinkage stabilizes noisy off-diagonal correlations but is not an optimized shrinkage estimator. Later qualification should compare Ledoit–Wolf/nonlinear shrinkage, factor covariance, conditioning, out-of-sample risk forecasts and stress behavior on identical datasets. Monitor PSD and eigenspectrum; covariance risk alone misses gaps and non-linear exposures.

### As-of data caveat

Historical OHLCV rows cut at date T do **not** prove that an adjusted vendor value was known on date T. Revision-time and corporate-action-adjustment metadata must be captured at ingestion. The analysis ID attests only to the current byte-level input view, not legal ownership or historical knowledge of those prices.

### Ingestion behavior (second integrity improvement)

`data.validate` now rejects nonfinite/missing OHLCV values instead of silently
dropping bars or replacing missing volume with zero. `lab analyze` reads the
stored Parquet rows without additional cleaning, then applies strict as-of
validation. **Existing datasets originally ingested with the older lossy
validator must be re-imported from their upstream source**; that history
cannot be reconstructed from already-modified Parquet. Provider correction
history and sealed holdout access are still not guaranteed at the storage
boundary. An in-memory post-cutoff filter is a leakage guard, not true
holdout file isolation.

## Next work: evidence-first ML

1. **Trusted data foundation:** point-in-time exchange instruments, final bars, timestamped provider revisions, calendar and corporate actions, metadata contracts and immutable staged datasets. Correct survivorship and delisting returns.
2. **Feature store:** timestamp all observations by *event time* and *available/known-as-of time*. Test each feature on synthetic future shocks and delayed feeds, split/symbol changes and half-day sessions. Cache by dataset/config/source hashes.
3. **Forecast baselines:** start with naive/linear, trend, EWMA and factor models; use supervised cross-sectional models only after stable labels, cost targets and feature provenance exist.
4. **Probabilistic models:** quantile/interval forecasts, residual distributions, conformal calibration with temporal validity checks, drift monitoring, and explicit abstention when data is novel or outside training support. Nonstationarity invalidates easy calibration claims.
5. **Comparative validation:** nested rolling/walk-forward, purge/embargo overlapping labels, unmatched-history tests, complete trial multiplicity, uncertainty under costs, regime stress and sealed holdout.
6. **Safe portfolio use:** only promote a well-calibrated model artifact with immutable weights/code/feature definitions, bounded predictions and independent single-use risk permits. A model failure or missing feature implies skip session, not guess.
7. **Research feedback:** label AI hypotheses as proposed/invalid/inconclusive/supported; log all attempts and negative evidence, data/compute budget, and reviewable artifacts. Never optimize against the sealed holdout by repeated prompting.

## Acceptance proof for this initial slice

- A future OHLCV modification does not change historical analysis at T.
- A historical edit changes the fingerprint.
- Incomplete/unaligned panels, invalid prices and impossible feature parameters fail closed.
- Correlation/covariance remain finite/PSD; perfectly proportional histories yield predictable shrunk correlation.
- Zero-variance series return null correlations rather than NaN; all-up and no-loss series have valid tail statistics.
- CLI disallows post-training holdout analysis, and all reports are explicitly exploratory.
- CI tests Python 3.13 and 3.14; this still does **not** prove the broker-connected application is deployable.

**Do not wire exploratory diagnostics directly into `RiskAgent`, `ExecutionAgent` or auto-promotion**. The next contract should separate verified features, probabilistic forecasts, portfolio proposals and independent risk permits.
