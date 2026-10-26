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

| Gate | Foundation / exit condition | Evidence to collect | Maps to (not automatically complete) | Status |
| --- | --- | --- | --- | --- |
| F0 | Reproducible, **immutable candidate baseline**: frozen dependencies, collaboration, CI and packaging | Record the exact candidate commit + dependency locks + CI run IDs + artifact SHA-256 in a write-once release manifest / provenance attestation; independently verify Python 3.13/3.14, Docker and lint. Before release Claude/release owner must pin an immutable reviewed release commit/tag or signed artifact, never a moving branch alias. New code creates a **new candidate record**, not retroactive invalidation of the previously attested one | D0; `code-review`, `operations-and-build` | **provisional** — last successful parent `6caca20` is not a release artifact; current candidate must pass CI and be pinned |
| F1 | Economic conservation: true fills only; exact cash/equity, zero recovery, short liabilities and fee/funding/corporate-action parity | Independent Decimal ledger versus native Nautilus portfolio/cache/account; cash merger, bankruptcy, short, delayed settlement, duplicate, restart and zero-value regressions | D1, D2; `market-data-and-execution`, `broker-lifecycle` | **blocked** — FP-01 phantom bars prohibited, but native corporate-action settlement not implemented |
| F2 | Qualified point-in-time inputs: source, instrument, bitemporal availability, revisions, missingness, lineage and **clock integrity** | Verified raw custody, data coverage/gap audits, independent mark/funding/quotes/corporate actions; record venue event time, recorder receipt/availability time, and broker gateway receipt time separately; enforce per-stream sequence and receipt monotonicity, measurable clock offset/uncertainty, bounded skew under a documented venue-specific policy, and quarantine ambiguous or backwards observations | D2, D7; `market-data-and-execution` | **partial** |
| F3 | Unified decision/risk contract: `ABSTAIN` for openings under uncertainty; hard limits for held + pending + partial fills; **strict reduce-only preservation** | Typed `TradeDecision` binding as-of times, thesis, policy, costs, uncertainty, budget and order plan; verified current holdings, pending-order quantities and broker lifecycle constrain close/reduce quantities so an intended reduction cannot reverse into exposure. A stale price or incomplete research feed **alone** must not block a demonstrably safe reduction; verified position/order ownership and venue-safe execution mechanics remain mandatory | D1, D4, D7; `adaptive-product` | **partial** — equities preflight and crypto reference oracle have distinct proven bounds |
| F4 | Execution and journal state-machine parity: intent != acknowledgement != partial fill; recover fail closed | Crash/restart, cancel/fill race, late/duplicate fill, unknown state, multi-currency/account reconciliation, single-writer lease and torn-power restore; broker timestamp is an independent recorded clock, not assumed equal to exchange or recorder time | D1, D3; `broker-lifecycle`, `operations-and-build` | **partial** offline; broker recovery blocked |
| F5 | Reproducible model qualification before exposure | Durable per-trial reservation before evaluation, selection-adjusted null/holdout/cost/volume tests; independent statistical calibration, deployment-context-pinned approval and stale-candidate rejection | D7; `research-calibration`, `adaptive-product` | **partial** — selection and built-in approval guards exist; population power calibration outstanding |
| F6 | Qualified broker demo (no real money) for explicitly supported venue/account/region | Authenticated user-authorized demo orders, partial fills, margin, actual fees/funding, stop/halt, clock-skew evidence, restart/reconciliation and supervised repeat sessions | D3; `broker-lifecycle`, `market-data-and-execution` | **blocked** |
| F7 | Production-grade operator and deployment workflow | Arch + Windows fresh-user installs, guided setup, data-health/risk/authority visibility, alerts, consent, maintenance, durable backup/restore, permission and soak tests | D4; `operations-and-build`, `independent-security` | **partial** |
| F8 | Independent adversarial audit and **scope-limited paper release** signoff | All seven `docs/release-criteria.json` gates independently satisfied for the exact release scope; no blocking reviews, pinned artifact and operator runbook. Explicitly list excluded markets/capabilities instead of misrepresenting them as complete | D0–D5/D7 as applicable to shipped scope; `code-review` plus all seven release gates | **blocked (0/7)** — D5 forex/alternate-equities remains blocked and excluded unless separately qualified; D6 live money stays **disabled**, not a paper-release requirement |

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
   availability time and risk/cost snapshot; deny **OPEN / INCREASE** when
   source, participation, authority or marks are unqualified. Preserve strict
   **CLOSE / REDUCE_ONLY** while a market-data feed is degraded **only if**
   independent broker/cache state proves owned position, outstanding-order
   exposure and a safe non-reversing quantity. Reduce-only must neither
   bypass venue/order lifecycle controls nor silently create a short or long
   after a late fill. If that evidence is missing, fail closed on new orders,
   cancel or reconcile pending intents, raise a critical operator alert and
   surface the manual broker-close pathway. A stale mark alone must not trap a
   validated reduction.
4. **Data quality and state:** qualify continuous independent trade/quote/mark/
   funding feeds, broker snapshots and crash-durable open-order evidence.
   Audit per-stream monotonic sequence, backward or duplicate receipt times,
   measured UTC clock synchronization and event-to-receipt/gateway skew.
   Out-of-order *exchange event times* can be legitimate; buffer, reorder or
   quarantine according to an explicit bounded policy, never invent timestamps
   or infer ordering across independent feeds. Persistent clock skew or unknown
   event availability must block new exposure and flag operator intervention.
5. **Evidence before optimization:** expand property/fault tests, selection
   calibration, independent holdout/power, net-cost edge qualification; only
   then introduce additional algorithms and broader venue adapters.
6. **Supervised operator trials:** credential-free Arch acceptance first, then
   user-authorized paper-account trials. Do not run real-money orders.


## Required acceptance scenarios for the refinements

- **Reducing with a stale quote:** with an independently reconciled long of 10
  shares and zero pending orders, a supported close of <=10 shares must remain
  eligible for execution risk checks even if the *research feed* is stale.
  It is not an instruction to submit a market order without venue price/order
  protection. Reductions are subject to broker and session safety checks.
- **Late fill race:** 10 long shares with an unconfirmed pending SELL of 10
  must not allow another SELL of 10 to be classified as `REDUCE_ONLY`.
  Cancel/verify or reconcile broker state first. For a short position the
  symmetric BUY rule applies. Never assume `cancel requested` = `cancelled`.
- **Unknown position or order:** if position ownership, pending fills,
  instrument capability, or account state is unknown, do not manufacture a
  "safe reduction" from the target alone; halt automatic exposure changes,
  keep reconciliation/alerts working and make the manual broker-close option
  prominent. A venue-native reduce-only flag helps when verified, but may be
  unavailable on some brokers and cannot be assumed.
- **Clock-skew faults:** backward or duplicate receipt timestamps, a vendor
  event timestamp after locally observed availability beyond the **measured**
  clock uncertainty, inconsistent gateway ordering, or an unknown clock
  offset must quarantine decision evidence, block opening, and preserve the
  operator's verified reduction/recovery options. Define tolerance and
  minimum time-sync evidence separately for each adapter; there is no safe
  global arbitrary five-second threshold.
- **F0 provenance:** a successfully tested commit is **not** yet a signed
  release. Check locked dependency digests, artifact SHA, GitHub CI results,
  independently reviewed candidate SHA and immutable release attestation.
  When a patch lands, create a new candidate manifest rather than altering a
  previous attestable record.

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
