# Causal probabilistic market research — implementation contract

**Implementation:** src/lab/predictive_research.py, via the lab forecast CLI. **Safety:** research-only; no broker, account, portfolio weight, promotion or order-sending dependencies. **Status:** statistical prototype, not proof of alpha or suitability for unattended trading.

## Causal features and labels

At the close of session t, the model constructs ten features from OHLCV through that session only: one-day log return; 5/21/63-session log momentum; 20-session RMS and downside volatility; 63-session drawdown; trailing standardized log volume; overnight gap; and log high/low range. No feature at t may read a later price. The forward label for horizon h is log(C[t+h]/C[t]) and only becomes known at the future session close. Input histories are filtered at the as-of cutoff before validation or estimation, then fingerprinted.

Point-in-time OHLCV calculations **do not establish** that an adjusted vendor price, corporate action, or instrument membership was actually known at that historical date. The current storage lacks an audited data revision history and physically isolated final holdout. Reports always state this limitation.

## Model and chronology

An intercept-bearing ridge model standardizes features using **training samples only**; regularization is positive. The training set is followed by a full h-session purge, a separate calibration block, another h-session purge, and a fixed historical test block. A training or calibration label may only be used if its future closing price is known before the next block begins. The horizon is 1–20 exchange sessions.

Calibration computes empirical residuals and a split-conformal absolute residual order statistic at the requested nominal coverage. Financial serial dependence means formal exchangeability and marginal coverage **cannot** be assumed. Empirical positive-return probability is derived from the calibration residual distribution; it is not proven calibrated.

The test reports log-return MAE versus the training-mean baseline, empirical interval coverage, Brier score versus training positive-frequency baseline, and a reproducible evidence ID. Model underperformance is preserved as a result, not concealed. After this fixed test, a separate model is refit using additional **mature** historical labels and a fresh calibration block to produce the as-of estimate. The one historical test is not a sealed external holdout or robust nested walk-forward qualification.

## Volatility regime estimation

The first regime model is an empirical two-state **filtered Markov volatility classifier**. Training daily returns set the median 20-session realized volatility cutoff, state-specific zero-mean Gaussian volatility scales, and Laplace-smoothed state-transition probabilities. The model then forward-filters observations chronologically using Bayes likelihoods; it never retrospectively smooths past states using future returns. The report includes filtered current high-volatility probability, model-implied next-session probability, training sigma estimates and transition probabilities.

This is **not** a fitted maximum-likelihood HMM, a reliable forecast of market direction, or an institutional risk model. Fixed Gaussian/stationary regime assumptions can be wrong.

## Drift, safeguards and user workflow

Each as-of feature is compared with the latest fit's training mean/standard deviation. Exceeding the configurable maximum z-score sets ABSTAIN_FEATURE_DRIFT. This is a simple novelty detector; more mature multivariate shift monitors are needed. Any incomplete/nonfinite OHLCV, invalid cutoff, inadequate labels, impossible interval coverage or unstable linear system fails closed. Reports carry RESEARCH_ONLY_NOT_FOR_TRADING regardless of their scores.

CLI examples:

- lab forecast --ticker GOOG --as-of 2010-12-30 --json
- lab forecast --ticker GOOG --as-of 2010-12-30 --horizon 5 --calibration 90 --test-sessions 100 --output results/forecast.json

JSON contains version, session, source/data/model hashes, deterministic analysis ID, model parameters, frozen test metrics, the current empirical interval, estimated probabilities, drift status and warnings. Outputs are created exclusively rather than silently overwritten.

## Next qualification work

1. Reliable point-in-time instrument/calendar/corporate-action data, vendor revision history and physically restricted final holdout.
2. Multiple purged rolling walk-forward folds with nested model selection, out-of-sample calibration, regime stress and **full trial history** for multiplicity control.
3. Compare naive mean/EWMA, volatility/factor baselines, boosted trees, distributional forecasting and more sophisticated HMM/state-space approaches **only when independently superior**.
4. Tests for genuine forward coverage, model drift, serially correlated labels, missing calendar sessions, stock splits/delistings, fees/implementation costs and unseen market regimes.
5. Immutable reviewed model registry, isolated AI code execution and independently authorized deterministic risk permits; these cannot be inferred from forecast quality.

Green CI, estimated probabilities and fitted regimes do **not** qualify automated paper or real-money trading.
