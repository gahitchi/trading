# Maker-study inference protocol

Pre-registered on `chatgpt/codex-maintenance` before any candidate recordings were examined. This protocol
implements roadmap A6b; it does not establish a profitable strategy or complete A8b qualification.

## Estimand and observations

The descriptive result remains the mean net markout per hypothetical fill, in basis points after the maker fee.
Its existing fill count, fill rate and mean are retained. They are not a sample of independent trades.

For uncertainty, use only the same valid, gap-free markouts whose complete interval lies within one UTC day.
An interval ending exactly at the next midnight is excluded. This prevents a horizon from sharing prices across
day clusters. Display the inference sample's count and mean separately from the descriptive sample.

Apply the same rule independently to the all/favours/neutral/against groups. No group, day or observation is
selected by its profit, and input order does not affect the estimate.

## Fixed estimator: utc-day-cluster-hac7-v1

Let N be the number of eligible fills, G the number of UTC days with eligible fills, m their fill-weighted mean,
n_d the fill count on day d, and S_d the sum of (net markout - m) for that day.

Use the cluster score variance V0 = sum(S_d squared). For calendar-day lags k = 1,...,7, add
2 * (1 - k/8) * sum(S_d * S_(d-k)), using only pairs of days actually separated by k calendar days.
Missing dates are not compressed into adjacent observations or counted as observed days.

Floor this Bartlett HAC score variance at V0, so a negative serial-covariance estimate cannot increase
confidence relative to day clustering alone. Multiply by G/(G-1), divide by N squared, and take the square root
to obtain se_bps. The adjusted statistic is inference_net_bps / se_bps.

Report the Kish effective day count, N squared / sum(n_d squared), discounted by V0 / max(V0, VHAC).
This is a concentration/dependence diagnostic, not a claim that this many days are independent.

The statistic is unavailable with fewer than 30 eligible UTC days, nonfinite data, or numerically zero
uncertainty. Thirty days is a project minimum, not a theorem guaranteeing valid inference. The naive fill-level
t remains visible as t_naive for comparison and cannot pass the A6 gate. The method version, seven-day lag,
minimum day count and midnight rule enter the trial fingerprint and the report stored in the trial log.

## Limits and acceptance

The seven-day bandwidth is fixed in advance; dependence beyond it is not fully captured. The estimate remains
an asymptotic diagnostic, especially with few or highly unequal day clusters. A8b still requires candidate
coverage, dependence and sensitivity checks, complete comparable trials, and multiplicity correction. A t value
does not automatically authorize paper or live trading. A3's seven-day/72-hour recording target is sufficient
to begin descriptive screening, but not to override this inference minimum.

Before adopting the implementation:

- Check a numerical example against exact rational arithmetic.
- Duplicate within-day fills: the adjusted estimate and effective day count must not change, while the naive
  t can inflate.
- Check UTC-midnight exclusion, actual calendar lags, insufficient days, degenerate uncertainty and group handling.
- Run a fixed synthetic null check: seed 20261029, 400 replications, 40 days, 20 fills/day, stationary Gaussian
  AR(1) day effects with coefficient 0.5 and independent fill noise. At most 5% may exceed the one-sided A6
  threshold t >= 3. This is a limited calibration check, not universal validation of the estimator.
- Verify the CLI logs the method, settings and all reported uncertainty quantities without inspecting holdout data.

Changes to this protocol after seeing results must have a new version and a recorded rationale; they must not
be used to relabel an inspected holdout as fresh evidence.

## Methodology references

- Cameron and Miller (2015), [A Practitioner's Guide to Cluster-Robust Inference](https://cameron.econ.ucdavis.edu/research/Cameron_Miller_JHR_2015.pdf):
  correlation within groups and the limitations of inference with few or unequal clusters.
- Newey and West (1987), [A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation Consistent
  Covariance Matrix](https://www.nber.org/papers/t0055): Bartlett-weighted covariance adjustment.

The day grouping, bandwidth, minimum sample and variance floor above are explicit project choices; the papers
do not prescribe those settings or certify these trading gates.
