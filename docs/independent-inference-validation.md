# Independent validation of daily diagnostic v1: pre-registration (A8b3)

Freeze `daily-hac-joint-block-v1` as implemented in `2d21dd8`. This protocol is registered before generating
its independent samples. The earlier 16/200 (8%) result remains evidence; it is not replaced or pooled away.
No method formula, lag/block grid, minimum, decision threshold or bootstrap seed changes in this validation.
No real candidate or holdout is inspected. The protocol ID is `daily-v1-independent-null-v1`.

## Questions and fixed execution

Evaluate the production procedure, including B=1999 and its existing PCG64 seeds 20261029+b, at p_joint <=0.05.
Each simulation's event is that any of its four strategies has p_joint <=0.05. Retain the minimum adjusted
p-value for every simulation; count events only after all registered repetitions are present.

Use 1,000 repetitions for each case below. Generate fresh data using PCG64 initialized from
`SeedSequence([20261030, case_id, repetition])`, with zero-based repetition 0..999. Bootstrap seeds remain
those of the frozen production method. The data streams differ from the earlier calibration's single stream.
Worker scheduling and resumption must not change generated values or outcomes.

For every repetition generate n+512 rows of five independent innovations. Gaussian innovations are standard
normal; heavy-tailed innovations are Student t with 5 degrees of freedom scaled by sqrt(3/5) to unit variance.
Start f at zero and iterate f_t=phi*f_(t-1)+sqrt(1-phi^2)*innovation_t, row by row. Discard the first 512 rows.
Make four strategy observations x_j=0.8*f_common+0.6*f_private_j. These matrices are the already-interior daily
samples supplied directly to the frozen joint diagnostic; boundary/date validation is covered separately.

| ID | Name | n | phi | Innovations | Assessment role |
|---|---|---:|---:|---|---|
| 0 | iid_minimum | 120 | 0 | Gaussian | Stationary primary case, minimum sample |
| 1 | ar05 | 240 | 0.5 | Gaussian | Stationary primary case, independent repetition of the earlier scenario |
| 2 | ar08 | 240 | 0.8 | Gaussian | Stationary primary case, longer persistence |
| 3 | ar05_t5 | 240 | 0.5 | Standardized Student t5 | Stationary primary case, finite-variance heavy tails |
| 4 | ar_negative | 240 | -0.5 | Gaussian | Stationary primary case, variance-floor sensitivity |
| 5 | variance_shift | 240 | 0.5 | Gaussian | Stress case: multiply the last n/2 strategy rows by 3 |

The variance shift is nonstationary and is registered as a stress result, outside the five stationary primary
cases. Publish it regardless of outcome; do not silently promote it to an assumption the method satisfies.
Finite burn-in approximates the stationary AR law and is fixed before testing.

## Assessment fixed before results

For k events among N=1000 in each stationary case, calculate a one-sided exact Clopper-Pearson upper bound U:

```
P[Binomial(N, U) <= k] = 0.01,  when k < N
U = 1,                       when k = N
```

The 0.01 tail is 0.05/5. The five upper bounds therefore give at least 95% simultaneous Monte Carlo coverage
under the binomial-repetition model by the union bound. Use inversion of the binomial CDF, not a normal
approximation or a new dependency. Verify zero/all-event cases and small exact examples numerically.
The stress case gets a separately labelled one-sided 95% upper bound and cannot change primary acceptance.

The registered primary validation requirement is **all five U <=0.05**. This is intentionally stricter than
accepting an observed rate near 5%: a method whose true rate lies at the boundary will usually fail to establish
the requirement at this finite sample size. Failure means insufficient evidence for the registered bound;
it does not by itself prove a true rate greater than 5%. Publish counts, bounds and each decision on failure.
Do not relax the requirement, remove cases, reroll seeds or change repetitions after seeing outcomes.

Even if all five bounds meet the requirement, keep `qualified=false`: this is validation on fixed synthetic
laws, not universal control, complete search-family coverage, qualified Sharpe inference or candidate
stationarity evidence. A8b and empirical gates remain open. A method revision after this suite needs a new
protocol and fresh independent validation; these samples then become development evidence, not a reused test.

## Persistence and reproducibility

Reserve a validation manifest before simulations. Record protocol content SHA-256, daily-method source hash,
package-source/runtime/dependency identity, all cases/settings and status. Checkpoint ordered per-case minimum
p-values atomically at most every ten completed repetitions and at each case's end. A stopped run resumes its
exact deterministic prefix; lost uncommitted work is recomputed with the same repetition seed. Re-running into
a new directory cannot generate a different favourable sample. Reject changed identities, corrupt prefixes,
unknown cases and missing completed evidence rather than mixing them into a report.

Concurrent invocations on the same directory are serialized by an exclusive local lock. Cases may execute in
separate processes, each owning its checkpoint. Archive the manifest, six case files and final summary. Final
assessment uses exactly the registered case set and completed prefixes; report missing work as incomplete.
No incomplete report may claim the primary requirement was met. A publication failure must leave existing
checkpoints available for resumption, and no command may delete failed results automatically.

The executable will be `python -m lab.inference_validation --output DIRECTORY [--workers N]`. Seeds,
repetitions and production resample counts are fixed, with no CLI settings for searching them. Unit tests may
use explicitly non-protocol sizes to exercise arithmetic/resumption cheaply; those cannot meet the full
registered validation. Full simulations run as a separate recorded validation, not on every unit-test run.

Methodological references: NIST's [exact binomial limits](https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm),
R's [Clopper-Pearson documentation](https://www.stat.math.ethz.ch/R-manual/R-devel/library/stats/html/binom.test.html),
and NumPy's [parallel RNG documentation](https://numpy.org/doc/stable/reference/random/parallel.html).
The AR laws, sample sizes and assessment requirement above are project choices, not prescriptions from those
sources. This harness is validation infrastructure; it does not change the diagnostic or any trading gate.
