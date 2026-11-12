# Bearish hybrid: prospectively registered, counted offline research (v1)

**Status:** synthetic software validation only. No new market-data ingestion, historical performance, qualified edge, executable paper order or live trade. This work sits on the draft sequence PRs #37–#40 and is intentionally isolated. Previous BTC losses and the protected 2026-05-08 UTC BTC holdout remain outside this new study.

The existing `lab.trials` / `lab.research_family` system already tracks previous attempts, freezes research-family generations, captures code and input provenance, and records failures. This change exposes a guarded entry point for the hybrid, instead of creating an incompatible private trial ledger.

## Frozen hypothesis and protocol

Construct a `StudySpec` with a **non-BTC USDT** symbol; complete UTC midnight research start/end; an earlier, independently frozen prospective protected boundary; and the *independently pinned* SHA-256 digest of an exact JSON archive. Its maximum window is 69 complete UTC days (bounded by the existing adapter's 100,000 candle limit). Supply explicit costs, virtual funding/collateral account, a `ProfitPolicy(reinvest_positive_fraction=...)`, and optionally signal/risk/replay settings. These values, the source pin and the (false-by-default) funding verification assertion all enter `planned_study_member(...)`.

**Before reading any candidate outcome**, register a family plan using existing `lab.research_family.register_family(results_dir, {"name": ..., "rationale": ..., "members": [planned_study_member(...)]})`. A family name/rationale should describe the data acquisition, prior inspected evidence, negative controls, primary statistic, and *all* planned symbol/time/reinvestment comparisons; do not register one member at a time in response to earlier returns. `register_family` preserves prior attempts, including unsuccessful ones. Trial multiplicity and inference remain unresolved until the full family is completed and the existing report gates are audited.

Calling `run_registered_study(results_dir, archive_path, spec, ...)` verifies exact membership in the **current** frozen family. Without it, it fails **before opening any archive**. Inside existing `trial_run`, an attempt is reserved durably before reading the pinned bytes. `InputEvidence.blob` records byte size and SHA-256; the archive adapter validates schema, causal as-of OI/funding information, protected boundary and exact symbol. It then rejects any partial/mismatched UTC window. The unchanged dynamic hybrid short simulator evaluates only after these gates. Failed and interrupted evaluations remain counted as attempts with an `unavailable` result. Completed attempts record summary numbers but always `qualified=False` and `archive_origin_authenticated=False`.

For example, in a caller-owned script (not executed by this PR):

```python
from pathlib import Path
from lab.bearish_hybrid import Costs, VirtualAccount
from lab.bearish_hybrid_dynamic import ProfitPolicy
from lab.bearish_hybrid_study import StudySpec, planned_study_member, run_registered_study
from lab.research_family import register_family

spec = StudySpec(
    symbol="ETHUSDT", start_ns=START_UTC_MIDNIGHT_NS,
    end_ns=END_UTC_MIDNIGHT_NS, boundary_ns=FROZEN_BOUNDARY_NS,
    archive_sha256=INDEPENDENTLY_PINNED_LOWERCASE_SHA256,
)
account = VirtualAccount(10_000, 10_000, 0, 0, 5_000, 0, "run", True)
costs = Costs(4, 3, 2, 1)  # illustrative bps, not exchange fill evidence
policy = ProfitPolicy(reinvest_positive_fraction=0.50)
member = planned_study_member(spec, costs=costs, account=account, policy=policy)
register_family(Path("results"), {
    "name": "Independent preregistered bearish hybrid study",
    "rationale": "State source protocol, prespecified windows, controls, primary endpoint and all alternatives",
    "members": [member],
})
report = run_registered_study(
    Path("results"), Path("/audited/archive.json"), spec,
    costs=costs, account=account, policy=policy,
)
```

Do not execute this illustrative example with guessed timestamps or an unverified source digest. **Register the complete planned family once**, not after seeing results. Never mix the existing historical negative candidates with a new holdout. BTCUSDT is deliberately refused in this guarded entry point; an independently justified BTC research program requires an explicit separate protocol.

## Limits and next gates

- SHA-256 proves exact byte identity, **not who authored the archive**, its completeness, its original arrival time, or its eligibility to be called unseen data. The external attestation digest required before `funding_verified=True` is also only a pin: neither the module nor the digest authenticates the attestation contents. The entry point records `archive_origin_authenticated=False` regardless.
- The upstream archived JSON does **not** itself provide verified historical OI publication latency, venue listing/turnover change history, reliable mark prices or exact funding payments. Unverified source inputs remain unqualified. Actual exchange-derived publication evidence, a source-acquisition log and independent checks are mandatory.
- The dynamic replay still models 1-minute OHLC reference fills, not L2/order queues, gap liquidation, partial fills, margin tiers, venue executions or actual funding settlements. A local virtual profit is not real cash. The strategy may lose money and does not guarantee a single profitable day.
- The registry and trial files are audit artifacts, **not cryptographically tamper-proof server attestation**. Copying results to a new directory or silently modifying artifacts can defeat untrusted standalone research; preserve immutable logs and an independent signed/git-reviewed registry outside the local process.
- Data collection must be built and independently audited before a historical edge can be claimed. The current harness does not fetch any historical files or validate venue ownership. Run negative controls and statistically appropriate inference with all planned configurations, then a genuinely protected out-of-sample test, before declaring anything qualified.

## Acceptance

`PYTHONPATH=src:. python -m pytest tests/test_bearish_hybrid_study.py -q`

`ruff check src tests` / `ruff format --check src tests` / `PYTHONPATH=src:. python -m pytest -q`.

These synthetic tests do not make a strategy profitable. The next actual data milestone is signed or otherwise independently verifiable historical acquisition with a provenance-preserving publish-time model for all features and no inferred publication timestamps.
