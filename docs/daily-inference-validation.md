# Daily dependence diagnostic validation: A8b2

Protocol: [daily-inference.md](daily-inference.md), published on the dedicated branch in
`3e45e7127579efb62d85a08e700bd56532faef0f` before implementation or synthetic calibration.
No real candidate data or holdout was inspected.

## Fixed dependent-null outcome

The exact registered calibration generated **16/200 (8%)** replications with any joint p <= 0.05.
That is exactly the pre-registered 16/200 engineering acceptance ceiling. It is above the nominal 5% threshold;
this limited experiment does **not** establish 5% error control, and the diagnostics remain **unqualified**.

There was no change to the formula, block lengths, sample minimum, seeds, resample counts or acceptance rule
after observing the result. Further error-control claims need an independently registered validation or method
revision. Repeatedly selecting a seed/block length until a favourable calibration appears is not qualification.
The observed count is retained even though all automated checks meet their stated acceptance criteria.

Environment: CPython 3.13.16, NumPy 2.5.4, pandas 3.1.0. Calibration: 240 daily observations, four correlated
strategies, stationary Gaussian AR(1) common/private factors with phi=0.5, seed 20261029, 200 repetitions,
B=499 and the fixed 7/14/28-day block sensitivity. The protocol gives the generation order and exact formulas.
Production report defaults remain B=1999, not the calibration's smaller Monte Carlo count.

## Numerical and software evidence

- The registered HAC numerical example matches mean 14.5, SE 3.913320899155871 and t 3.7052928634418265.
- A separately expressed row-by-row reference loop matches block maxima and plus-one p-values, including a
  batch boundary. Positive scaling, column ordering, duplicate columns and family growth checks pass.
- The inference sample always discards first/last dates, requires 120 interior consecutive UTC dates and keeps
  marked account-equity returns separate from carry's per-leg-notional observations.
- Strict history/provenance checks retain failed/reserved/legacy attempts as blockers, reject changed windows
  and disagreeing retries, and keep holdout attempts out of research calculations.
- Fast/carry CLI tests check logged daily series, explicit short-sample reasons, saved method/sensitivity/family
  reports and holdout separation. A derived-report write failure does not erase the already completed trial.

Individual daily reports are stored in each trial's `result.daily_inference`; carry additionally logs its daily
`return_stats` series. Joint research reports are atomically written to `results/inference/<attempt_id>.json`,
with method settings, participating attempt IDs and a SHA-256 of the scoped history snapshot, including on
unavailable reports when that history is readable. Primary evaluation evidence remains authoritative if
publishing a derived report fails. No report can set `qualified` to true in this implementation.

## Still open

A8b needs a complete prospective search family across candidate symbols/study types, an explicit treatment of
unavailable outcomes/cohort recovery, candidate stationarity and dependence sensitivity evidence, stronger
independent calibration and a qualified Sharpe-based selection criterion. This maximum-statistic tail area is
not a deflated Sharpe ratio or a probability of profit. Existing roadmap gate thresholds are unchanged.
