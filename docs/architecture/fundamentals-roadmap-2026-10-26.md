# Trading Lab — foundation-first completion gates

**Checkpoint:** 2026-10-26. **Branch:** `chatgpt/fundamentals-settlement-20261026`.
**Status:** DEVELOPMENT; **paper-only**. No real-money capability, broker certification or claim of positive alpha.

## Rules for measuring completion

A check mark means an invariant has reproducible code evidence **and** a linked
runner/host acceptance record at the exact commit. Added files and passing unit
tests alone are not release progress. Never turn a blocked gate green because a
similar *offline synthetic* scenario works. Keep supported stock-paper and
crypto-offline paths distinct until native engine/account parity is proven.

Gate dependencies are strict: F0 -> F1 -> F2 -> F3 -> F4 -> F5 -> F6 -> F7 -> F8.
Later investigations may proceed in parallel but cannot authorize execution
while an earlier gate is unresolved.

| Gate | Foundation / exit condition | Evidence to collect | Status |
| --- | --- | --- | --- |
| F0 | Reproducible baseline, locked dependencies, immutable collaboration, CI and packaging | All required GitHub checks green at candidate SHA on Python 3.13/3.14 and Docker | **provisional** (CI passed at parent `6caca20`; new commits require fresh CI) |
| F1 | Economic conservation: real fills only, exact cash/equity and zero recovery, short liabilities, fee/funding/corporate-action parity, no invalid data | Independent Decimal ledger versus native Nautilus portfolio/cache/account; cash merger, bankruptcy, short, delay, duplicate, restart and zero-value regressions | **blocked** — FP-01 phantom bars now prohibited; native corporate-action settlement not implemented |
| F2 | Qualified point-in-time inputs: source, instrument, timestamps, availability, source revisions, missingness and lineage | Signed/verified raw custody, no time travel, complete bounded gaps, independently sourced mark/funding/quotes and corporate-action revisions | **partial** |
| F3 | Unified decision/risk contract: abstain by default, hard operator ceilings and caps for held + pending + partial fills | A typed `TradeDecision` binding hypothesis, as-of times, policy, costs, uncertainty, budget, order plan, explicit `ABSTAIN`; same semantics in backtest and paper | **partial** — equities preflight and crypto reference oracle have distinct proven bounds |
| F4 | Execution and journal state-machine parity: intents != acknowledgments != partial fills; recovery fail closed | Crash/restart, cancel/fill race, duplicate/late order, unknown state, multi-currency/account reconciliation, single-writer lease, torn-power restore | **partial** offline; broker recovery blocked |
| F5 | Reproducible model qualification, before exposure | Pre-evaluation durable trial reservation; selection-adjusted null/holdout/cost/volume tests; independent statistical calibration; deployment-context-pinned approval; reject stale candidate | **partial** — selection and built-in-approval guards exist, population power calibration outstanding |
| F6 | Qualified broker demo (no money) under supported account/region and venue | Authenticated user-authorized demo account and supervised orders, margin, fee, funding, stop/halt, reconciliation and repeat sessions, signed evidence | **blocked** |
| F7 | Production-grade operator and deployment workflow | Arch + Windows new-user installs, guided setup, clear live data health, authority, consent, alarms, maintenance, persistent recovery, alert and soak tests | **partial** |
| F8 | Independent adversarial audit and paper release signoff | All seven `docs/release-criteria.json` gates independently satisfied; no unresolved blocking review, signed release build and operator guide | **blocked (0/7)** |

## Immediate engineering order (strict sequence)

1. **Settlement safety (this change):** remove fabricated post-delisting
   opening prints / fictitious volume and the arbitrary minimum price;
   refuse trading until corporate-action settlement maintains **both**
   native Nautilus account/position and independently audited ledger.
   On-disk vendor recovery records must reject NaN, infinite, negative and
   malformed fields. This closes the *unsafe simulation behavior*, **not** F1.
2. **Native corporate-action events:** design and test a separately classified
   settlement payable/write-off/cash-merger model using the pinned Nautilus
   API. Prove zero value and deferred settlement without synthesizing tradable
   quotes; reconcile native and independent state after restarts.
3. **Decision evidence:** define one immutable decision payload with actual
   availability time and risk/cost snapshot; deny openings with missing source,
   participation, authority or mark; conserve reductions.
4. **Data quality and state:** qualify continuous independent trade/quote/mark/
   funding feeds, broker snapshots and crash-durable open-order evidence.
5. **Evidence before optimization:** expand property/fault tests, selection
   calibration, independent holdout/power, net-cost edge qualification; only
   then introduce additional algorithms and broader venue adapters.
6. **Supervised operator trials:** credential-free Arch acceptance first, then
   user-authorized paper-account trials. Do not run real-money orders.

## Source references

- `docs/audits/2026-10-26-first-principles-audit-r2.md`, FP-01 through FP-07.
- `docs/audits/2026-10-24-quantitative-research-upgrade-audit.md`,
  especially F-R09: assumptions must not become executable settlement fills.
- `docs/architecture/multimarket-release-gates.json`: D0–D7 milestones
  (offline D1/D2 partial; authenticated D3 blocked; D6 live disabled).
- `docs/release-criteria.json`: seven independent external release gates.

## Operator acceptance and evidence requirements

New branch, no broker credentials required:

```sh
git fetch origin
git switch chatgpt/fundamentals-settlement-20261026
git pull --ff-only origin chatgpt/fundamentals-settlement-20261026
uv sync --frozen --extra dev
uv run --no-sync pytest -q tests/test_foundational_settlement.py tests/test_gemini_audit.py
uvx ruff check src tests tools
uvx ruff format --check src tests tools
python3 tools/collab.py check
```

The tested outcome must be a rejection of unsupported settlement, not a
profitably simulated cash merger. Do **not** run a paper gateway or touch a
production journal for this validation. Report the checked-out SHA, precise
command failures and environment. Hosted CI passing is a necessary condition,
not independent Arch or broker acceptance.
