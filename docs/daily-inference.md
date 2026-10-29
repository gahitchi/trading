# Daily dependence diagnostics: pre-registration (A8b2)

Registered before implementation, synthetic calibration or candidate inspection on
`chatgpt/codex-maintenance`. No real candidate or holdout is inspected in this work.
The identifier is `daily-hac-joint-block-v1`.

This adds diagnostics of positive mean daily net performance. It is not a replacement for the Deflated Sharpe
Ratio, an automatic gate, a probability of future profit, or complete A8b qualification. Statistical thresholds
in the roadmap are unchanged. Candidate stationarity, the full selection family across symbols/study types,
failed-outcome treatment, and a qualified Sharpe criterion remain open.

## Data and sample

Use UTC-date maps of net daily observations. Fast uses changes in net marked account equity
(`daily-net-equity-v1`); carry uses daily summed P&L per unit of leg notional (`daily-net-notional-v1`).
These bases have different economic meanings and must never be pooled, although both use daily frequency.
Carry must log the series rather than only a Sharpe summary. Existing economics and missing-data rules remain.

Require identical date sets and basis within a family, canonical ISO dates, consecutive calendar dates, and
finite interior observations. Discard the first and last calendar date for inference in every run, before
looking at outcomes, because recording boundaries may produce partial days. Require at least 120 remaining
dates. A7a still governs exposure across recording gaps. The sample minimum is a project safeguard, not a
literature guarantee or a change to A3's seven-day descriptive screen.

## Daily-mean uncertainty

For n observations x_i, mean m and residuals e_i=x_i-m, for L in {7,14,28}:

```
V0 = sum(e_i^2) / (n-1)
VL = [sum(e_i^2) + 2 sum_{k=1..min(L,n-1)} (1-k/(L+1)) sum_{i=k..n-1} e_i e_{i-k}] / (n-1)
SE_L = sqrt(max(V0, VL) / n)
SE = max(SE_7, SE_14, SE_28)
t_daily = m / SE
```

The variance floor prevents negative covariance from increasing confidence over the IID variance; taking the
largest SE across fixed bandwidths is a conservative sensitivity policy. Zero or numerically degenerate SE
is unavailable, including a constant zero series; no infinite-confidence result is emitted. A short or invalid
sample reports an unavailable reason. These choices do not prove that dependence beyond 28 days is absent.
Report the inference dates/count, mean, SE, t and each bandwidth's estimate.

## Joint research-family diagnostic

For a matrix X of those same dates by every comparable configuration in the existing (study kind, instrument)
research scope, use the following single-step maximum statistic:

1. Every attempt remains visible. A reserved, failed, unavailable or legacy/unidentified attempt blocks the
   diagnostic. Do not pick a successful retry after a failed attempt. Require at least two distinct settings,
   identical guarded input/code/runtime identities, identical result windows, and logged daily series.
2. Repeated identical settings/identity may be deduplicated only if their daily series and result windows agree.
   A changed result from the same identified inputs is unavailable. Sort configuration fingerprints for stable
   reporting. Holdout attempts never enter or tune the research family.
3. Center each column at its own sample mean. For block lengths b in {7,14,28}, independently seed NumPy PCG64
   with 20261029+b. Draw ceil(n/b) uniform circular block starts in [0,n) for each replication; concatenate
   consecutive calendar row indices modulo n and truncate to n rows. Use the identical indices for every column.
4. Use B=1999 replications. For each replication calculate the centered resampled column means divided by their
   original-sample SE above, and take the maximum across all columns. SE is fixed within a block-length run;
   this is not bootstrap re-studentization or Romano-Wolf stepdown.
5. For each column j report p_b,j = (1 + count(max_statistic >= observed_t_j)) / (B+1), including ties.
   Report p_joint,j = max(p_7,j, p_14,j, p_28,j). Report all sensitivities, settings and Monte Carlo resolution.
   A duplicate return column cannot increase confidence; adding a comparison cannot lower the existing columns'
   adjusted p-values when the date indices are unchanged.

This captures observed cross-configuration correlation through joint resampling, without an estimated
“effective number of independent trials.” It is a conditional block-bootstrap tail-area diagnostic under a
centered null of nonpositive expected daily performance. Fixed bandwidths/block lengths and finite samples do
not supply universal error control. Calling 1-p a probability of an edge is prohibited. A complete,
prospectively frozen search family and candidate-specific stability/block-length checks are still required.
No history/cohort exclusions or recovery mechanism are introduced to remove unsuccessful trials.

## Validation fixed before calibration

- Numerical example: x=0..29, L=7. Mean 14.5; SE 3.913320899155871; t 3.7052928634418265 under the stated formula.
- Compare block indices, maxima and plus-one p-values against a small, separately expressed reference loop.
- Check positive scaling, column permutation, identical-column duplication and adding a comparison; report
  finite-sample invariance within numerical tolerance. Check missing dates/values, different bases, short and
  constant samples, provenance/window changes, disagreeing retries, and holdout exclusion.
- Fixed dependent-null calibration: PCG64 seed 20261029; 200 replications; 240 dates; four strategies. Generate
  independent stationary Gaussian AR(1) common/private factors with phi=0.5, standard-normal initial states
  and innovation scale sqrt(0.75). Each strategy is 0.8*common + 0.6*private, so contemporaneous correlation is
  0.64 and the null mean is zero. Use B=499 for this calibration and the same three block lengths/SE policy.
  Count replications in which any p_joint <= 0.05. Accept this limited synthetic check only if <=16/200 (8%).
  Publish the count even on failure. Do not tune the method/seed/acceptance rule after seeing it; failure leaves
  A8b2 unqualified and requires a documented new protocol before a new calibration.

Unit-test sample sizes/resample counts may be smaller to verify arithmetic. Production CLI settings are fixed
above and logged in the reports. A passing synthetic check is evidence for this specified null model only.

## Methodological basis

- Newey and West (1987), [positive semidefinite HAC covariance estimation](https://www.nber.org/papers/t0055).
- White (2000), [A Reality Check for Data Snooping](https://doi.org/10.1111/1468-0262.00152).
- Romano and Wolf (2005), [Stepwise Multiple Testing as Formalized Data Snooping](https://doi.org/10.1111/j.1468-0262.2005.00615.x).

Those papers motivate HAC and joint resampling/multiple-comparison discipline. This project's fixed floors,
minimum, sensitivity grid, circular resampling and fixed normalization are explicitly specified choices;
the implementation does not claim to reproduce the papers' full methods or inherit an unconditional theorem.
