# Trial evidence (roadmap A8b1)

A statistical diagnostic must not quietly select only successful evaluations. This branch reserves each CLI
`micro-study`, `maker-study`, `fast backtest` and `carry study` evaluation before its evaluator loads inputs.
The automatically evaluated cash-carry “always in” comparison is a separate attempted configuration too.
Syntax, setup and window checks happen first; no evaluation starts if its reservation cannot be written.
Boundary discovery may inspect timestamp metadata before reservation, as in the existing holdout protocol.

## Durable lifecycle

`results/research_attempts/<attempt_id>.json` is one atomically replaced, fsynced state file per evaluation.
Its frozen settings, parameter fingerprint, requested window and source/runtime identity exist before work
begins. This avoids serializing all research evaluations behind one long-lived process lock.

- `reserved`: the evaluator may be running or may have died without cleanup. An unknown outcome is an attempt.
- `completed`: the result, actual result window and loaded-input manifest were saved atomically.
- `failed`: an exception, interruption or missing/invalid result prevented completion; an unavailable result
  and the partial input manifest are retained. A failed outcome write leaves the durable reservation instead.

Terminal states are mirrored into the existing `fast_trials.jsonl`. Readers merge that log, legacy entries and
all attempt files, deduplicating each attempt ID. If mirroring fails after completion, the atomic completed
file still contributes its result and configuration. A hard process exit before completion leaves a counted
reservation. Re-running a configuration adds another attempt and does not erase the original failure.

Corrupt/truncated JSON, duplicate keys, nonfinite numbers, bad manifest digests, conflicting mirrors and unknown
attempt versions stop history processing. Readers do not silently discard them. Keep both the attempt directory
and JSONL log when copying or backing up research evidence. Do not delete attempts or trim a corrupted log to
recover a favourable score; restore the evidence from a verified backup and retain the incident record.

Holdout evaluations additionally retain A9a's serialized reservation and reuse rules. A failure of a research
attempt or its result publication inside a holdout evaluation cannot make the holdout unused again. Research
attempt files and holdout reservations are complementary; neither replaces the other.

## Source and runtime identity

Each attempt records SHA-256 digests of the on-disk `lab` Python sources and their combined manifest, Python
version/implementation, installed versions of NautilusTrader/NumPy/pandas/PyArrow, and available `pyproject.toml`
and `uv.lock` digests. Git commit and working-tree dirty status are recorded when Git is available; source
content identity still works for wheels, source archives and uncommitted edits.

The displayed configuration count distinguishes settings and source/runtime identities. Repeated attempts of
the same settings under the same identity remain one distinct configuration, while every attempt is retained.
Legacy entries have unknown source identity and remain counted separately from identified new evaluations.
Git commit alone does not identify uncommitted code, and a content hash alone does not archive its source.

## Loaded-input identity

The manifest hashes the actual in-memory inputs supplied to the evaluator, rather than catalog filenames,
modification times or future data files. Its method is `sha256-canonical-records-v1`:

1. Each stream has a unique name and ordered records. JSON is encoded with sorted keys, compact separators and
   finite JSON numbers; each record's bytes are prefixed by their unsigned eight-byte big-endian length.
2. Native market objects use their `to_dict()` representation, retaining receive/exchange timestamps,
   instrument identifiers, sequences and exact formatted prices/sizes. Fast replay also hashes its selected
   instrument definition; maker study hashes the definition when tick improvement uses it. Micro study hashes
   the leader's actual book stream as well.
3. Carry hashes the normalized long, short, funding and turnover series after the existing window truncation,
   before hourly study calculations. Each record contains UTC nanoseconds and `float.hex()` values, including
   explicit nonfinite values. A one-ULP value change is distinguishable. Absent series have an explicit sentinel.
   Cross-venue funding is the derived funding difference consumed by the study, not an archive of both raw files.
4. Each stream records count, timestamp bounds, completion status and SHA-256; the ordered stream manifest is
   hashed again. A failure while hashing retains a partial stream. A hard exit can leave input identity unknown.

Research does not hash held-out prices to identify its inputs. Appending unseen market data or changing
cached carry prices after the research boundary leaves the research digest unchanged. Holdout inputs are
identified only inside the existing holdout reservation, through its frozen upper cutoff; carry may use prior
history for the already-documented signal warmup. These are input identities, not copies of the datasets.

## Diagnostics and remaining qualification

Counts and diagnostics read authoritative attempts as well as legacy history. Any reserved, failed or
unavailable research attempt blocks its scope's provisional Sharpe diagnostic. A later successful retry cannot
hide it. New identified trials with changed input content, source/runtime identity, incomplete manifests or a
mixture of identified and unidentified history cannot be presented as comparable trials just because their
window timestamps match. Carry also refuses missing trial statistics or different windows instead of dropping
those configurations. Holdout attempts never tune research diagnostics.

Legacy-only IID diagnostics retain their provisional mathematical behavior; their provenance is unverified,
and they cannot pass a trading gate. A8b remains open: register and validate the treatment of serial dependence,
correlated trials, candidate selection and unavailable outcomes before inspecting candidate results. A future
cohort or recovery rule must account for the preserved history; erasing it or simply taking the latest
successful result is not a qualification method. No profitability threshold is changed by A8b1.

The tests cover forced process exit, concurrent attempts, reservation/outcome/append failures, strict history
parsing, retained failed retries, all four CLI reservation paths, exact content changes and holdout exclusion.
They establish local process-crash behavior using the existing persistence helpers, not guarantees about every
filesystem or hardware power-loss mode. The evidence is a reproducibility aid, not a signed, tamper-proof audit
service, an immutable source snapshot, or proof that modules could not be monkey-patched while running.
