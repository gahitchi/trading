# Prospective search coverage (A8b4)

`lab research register --plan plan.json` freezes one search plan for the configured results directory.
`lab research report` audits **every research attempt** there, across instruments and all four study kinds.
There is no scope filter, plan replacement, amendment, successful-retry recovery or history reset command.
This is an evidence prerequisite, not statistical inference or a trading gate. `qualified` is always false.

The existing joint daily diagnostic still covers only its `(study kind, instrument)` scope. Its saved report
now labels that scope explicitly with `whole_search_adjusted=false`. Registering a broader plan does not adjust
those p-values for selection across instruments or study types. The [failed v1 validation](validation/daily-v1-independent/README.md)
and its frozen method remain unchanged. A qualified Sharpe criterion, an inference method covering the full
selection family, a new protocol and fresh independent validation remain open under A8b.

## Plan and workflow

A plan is strict finite JSON with exactly `name`, `rationale` and a nonempty `members` list. Each member has:

- `kind`: `micro-study`, `maker-study`, `fast-backtest` or `carry-study`.
- `instrument`: the exact trial identifier, e.g. `BTCUSDT-PERP.BINANCE` or `carry-cash`.
- `params`: the complete settings dictionary passed to the trial reservation, including model/gap policies,
  risk settings, costs and symbol lists where applicable. Do not supply only parameters that differ from defaults.
- `window`: the requested `[start_ns, end_ns]` in UTC nanoseconds; start may be null, end must be finite.
  These are reservation bounds, not the possibly shorter actual result span. Holdout boundaries still apply.

Exact duplicate members, unknown fields/kinds, invalid times, duplicate JSON keys and nonfinite numbers are
rejected. The JSON can be generated using existing settings helpers without running an evaluator. For example,
after downloading carry data, the following prepares a **single-configuration** plan, without inspecting returns:

```python
import json
from pathlib import Path

from lab.carry_study import CarrySettings, boundary, params_of
from lab.config import load_config

cfg = load_config()
settings = CarrySettings(top=10, min_turnover_m=20, slippage_bps=5)
symbols = ["BTCUSDT", "ETHUSDT"]  # freeze the intended universe before any evaluation
cutoff = int(boundary(cfg.results_dir, cfg.data_dir / "carry").value) - 1
plan = {
    "name": "carry research",
    "rationale": "Evaluate a fixed universe and settings before considering any survivor.",
    "members": [{
        "kind": "carry-study",
        "instrument": "carry-cash",
        "params": params_of(settings, symbols),
        "window": [None, cutoff],
    }],
}
Path("plan.json").write_text(json.dumps(plan, allow_nan=False, indent=2))
```

This is a schema/workflow example, not a completed B2 run or a replacement for its registered full universe.
Include **all** planned configurations, instruments, study kinds and cost stresses in the list before registering.
When cash carry runs with `top=0`, its automatic always-in comparison must also be planned separately (same
settings except `entry_apr=exit_apr=-1e9`). Unplanned comparisons are still counted and block coverage.
Boundary discovery uses the existing metadata path; registration itself reads no market inputs or holdout returns.

```sh
lab research register --plan plan.json
lab carry study --symbols BTCUSDT ETHUSDT --top 10 --min-turnover 20 --slippage-bps 5
lab research report
```

Once registered, normal CLI evaluations automatically link to the family **before** reading evaluator inputs.
No optional flag can exclude an inconvenient trial. Unexpected settings, instruments, study kinds and windows
are allowed to run as exploration, but remain linked and block prospective coverage. This preserves negative
evidence instead of preventing an unplanned evaluation from being recorded. Identical retries retain all attempt
IDs without increasing the distinct-configuration count.

Earlier research history is copied into the immutable registration, including failures, reservations and legacy
rows. Registering after research begins is labelled **retrospective** and cannot establish prospective coverage,
even after a successful retry. Running an earlier reserved attempt to completion is allowed; it remains prior
evidence. Do not move work to a fresh results directory, delete history or change the research universe to
discard earlier evidence. This first implementation intentionally supplies no statistical recovery/exclusion rule.

Holdout attempts remain separate under A9a. They are not members, coverage observations or inputs to research
inference. This audit never authorizes a holdout test or replaces the holdout ledger.

## Audit result

`results/research_family_report.json` is a derived snapshot of the registration and authoritative trial history.
It records registration/plan/history digests, all research attempt IDs, prior and legacy counts, full planned
members with their attempt IDs, distinct configurations and explicit blockers. `lab research report` exits 1
when coverage is blocked and 0 when the coverage checks complete; **neither means a strategy qualified**.

Prospective coverage requires all planned members to have evaluations, no earlier research or new legacy rows,
no unplanned/unlinked attempts, the registered source/runtime identity, and complete identified outcomes.
Failed, reserved, unavailable, empty-result or incompletely identified evaluations block it. A later successful
retry cannot clear an earlier failure. Repeated settings must use the same actual input content and result
window; exactly identified repeated evaluations must also agree on their outcomes. Losing results are valid
coverage evidence and cannot be replaced by a more favourable retry. Different planned members may consume
different inputs; the audit does not declare their returns statistically comparable or pool their bases.

## Persistence and limits

`research_family.json` atomically retains the frozen plan, registration ID/time, source/runtime identity,
pre-existing research evidence and SHA-256 digests. `research_family.lock` serializes registration and trial
reservation, not the duration of evaluations. Competing registration processes cannot replace the first plan;
a concurrent trial is either prior evidence or durably linked before evaluator input access. Crashes retain
their linked attempt reservation. Corrupt/missing registration with surviving linked attempts prevents further
reservations; it cannot silently create a replacement family. Corrupt history stops the audit. Prior terminal
evidence missing or changed since registration is detected, even if both its original local copies were removed.
Derived-report publication failure does not alter the registration or underlying attempts.

Keep the registration, attempt directory, trial JSONL and any external research records together. The guarantee
is local software coverage of this results directory, not tamper-proof storage, detection of every separate
directory/manual experiment, or proof that an operator had never viewed data. Deleting all evidence, rewriting
all hashes or studying outside these commands cannot be prevented by local files. No empirical candidate,
synthetic method revision, inference acceptance threshold, execution model, risk control or order path changes
in A8b4. The next inference protocol must account for this entire search history rather than treating the old
per-symbol diagnostic as a full family adjustment.
