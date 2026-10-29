# Independent daily-v1 validation: registered requirement not met

Protocol [A8b3](../../independent-inference-validation.md) was published in `66129e9` before sample generation.
The harness and all 286 then-existing software tests were published in `e880b431bdf26e3025a59dca193d3b6706f664bb`
before this full simulation run. The daily diagnostic's source hash remained
`048c8136891340f2a92b8a50eecb8bff7e118e3cf815ec3c0ac1c57d6d2aa94d`, exactly the frozen `2d21dd8` version.
No formula, seed, sample size, block grid, assessment rule or case was changed after results were observed.
No real candidate or holdout was inspected.

All **6,000 registered repetitions completed**, each with the production B=1999 bootstrap replications.
The registered primary requirement was **not met**. `qualified` remains false.

| Case | Flagged / 1,000 | Observed rate | One-sided upper bound | Confidence | Primary requirement |
|---|---:|---:|---:|---:|---|
| IID, minimum 120 dates | 48 | 4.8% | 6.611% | 99% | Not met |
| AR(1), phi=0.5 | 93 | 9.3% | 11.652% | 99% | Not met |
| AR(1), phi=0.8 | 135 | 13.5% | 16.209% | 99% | Not met |
| AR(1), phi=0.5, Student t5 innovations | 79 | 7.9% | 10.106% | 99% | Not met |
| AR(1), phi=-0.5 | 20 | 2.0% | 3.289% | 99% | Met for this case only |
| Variance shift, nonstationary stress | 67 | 6.7% | 8.148% | 95% | Outside primary assessment, as registered |

A flag means any of the four strategies had p_joint <=0.05. Exact Clopper-Pearson upper bounds use a 0.01 tail
for each of the five primary cases, giving at least 95% simultaneous Monte Carlo coverage under the registered
repetition model. The requirement was every primary upper bound <=0.05. An observed 4.8% rate alone cannot
establish that requirement; the uncertainty bound matters. The higher observed rates under persistence and
heavy tails reinforce that this frozen diagnostic cannot justify 5% error-control claims.

This independent result does not erase or pool away the earlier [16/200 calibration](../../daily-inference-validation.md).
It does not identify a trading edge, qualify a Sharpe statistic, or complete Phase 1. Future method changes
need a new pre-registration and fresh independent validation. Reusing these samples to develop a revision
would make them development evidence, not a new independent test.

## Evidence

- `manifest.json`: protocol, frozen method/package/runtime/dependency identity and complete run history.
- Six case JSON files: all ordered minimum joint p-values, prefix digests and interruption records.
- `summary.json`: counts, exact bounds, case-file hashes and the failed registered assessment.

The source/runtime manifest identifies the actual producer `e880b43`; the final branch adds CLI disclosure of
this result after execution. That disclosure does not change the frozen diagnostic or generator. Checkpoint
resumption intentionally refuses different package/runtime identities. For exact archived identity use the
producer revision; running the current package in a fresh directory recreates the same deterministic data,
not a new independent validation sample:

```sh
uv run --no-sync --python 3.13 python -m lab.inference_validation --output /tmp/daily-v1-independent-reproduction --workers 6
```

The command's successful completion means the validation executed and retained evidence, not that its
statistical requirement passed. Read `primary_requirement_met` and `qualified` in `summary.json`. No command
promotes a synthetic result to a trading gate. Software unit tests verify the harness and evidence integrity;
they do not turn this failed statistical assessment into a pass.
