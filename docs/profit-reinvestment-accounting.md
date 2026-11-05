# Variable daily P&L and partial-profit reinvestment — accounting contract

**Decision: 2026-11-05.** The objective is not equal profits on each
day. A short-duration strategy may make a few cents, have much larger
profitable days, and incur losing days. The **whole-period,
cost-adjusted equity curve must grow** under risk-limited conditions
and independently validated evaluation. A losing day is acceptable,
but profit on another day is **not guaranteed to compensate for it**.

This is a software-accounting specification, not a new backtested
strategy, live-trading authorization, profit forecast or registered
research family. The existing BTC holdout and all previous
registrations remain unchanged.

## Implementation checkpoint — 2026-11-05

A **pure, dependency-free research accounting kernel** is implemented
at `src/lab/profit_allocation.py` with `33` focused tests
(`tests/test_profit_allocation.py`), all passing under local Python
3.13. It has been committed on `chatgpt/codex-maintenance` but
**Termux acceptance, repository-wide regression and downstream
replay/paper integration are still pending**.

Current implemented behavior:

- Decimal-string money and strict currency/precision checks (no floats);
  frozen, explicitly supplied reinvestment fraction and currency unit.
- Every completed, audited trade asserts
  `net = gross - fees - slippage + funding`; only *positive
  realized net trade profit* can be fractionally reallocated.
  Costs/funding booked in the trade must not be booked again
  as standalone adjustments. Stable source IDs prevent exact
  event-source replay under a different event ID.
- Losses reduce deployable capital and equity; retained profits
  do not automatically become trading capital. Explicit reserve
  transfers preserve total equity; withdrawals cannot spend
  retained reserves implicitly.
- UTC day close requires an explicit midnight **absolute
  unrealized-P&L mark** for every day. Missing days, non-UTC
  timestamps, reversed events, invalid economics and non-quantized
  amounts fail closed. Across-midnight releases of already
  marked profit/loss are accounted for without double counting.
- Reports show daily realized and unrealized deltas, net P&L
  (excluding external flows), cumulative wins/losses, equity,
  drawdown, deployable and retained capital and an absolute
  risk-capacity bound. Without a configured deployable cap,
  risk capacity is **unavailable**, not unconstrained.
- Isolated research kernel only: **no durable event journal yet,
  no exchange wallet position ledger, per-instrument margin
  engine, actual custody segregation, broker connectivity,
  trade execution, FX conversion or live order permission**.
  It does not automatically derive a reliable `released_mark`
  from filled position quantities; the upstream reconciler
  must provide a correctly attributed value.

## Durable journal implementation (2026-11-05)

`src/lab/profit_journal.py` now implements an append-only
accounting **event journal** for paper/research use, with a separate
`tests/test_profit_journal.py` regression suite. **These new
journal tests and their integration with the prior 33 passing
kernel tests are awaiting Termux/Arch qualification.**

- An exclusive process lock serializes writer operations. Each
  append reconstructs and validates the exact ledger state before
  admitting an event. Persisted JSONL records contain monotonic
  sequence numbers, stable event/source IDs, an exact frozen
  reinvestment policy, a predecessor SHA-256, and the record
  SHA-256. Duplicate economic sources cannot be rebooked merely
  under a different event ID.
- A separate fsynced `.head.json` checkpoint records the
  latest committed sequence/hash; the JSONL file alone is not
  allowed to silently lose a complete trailing event. Checkpoint
  and record disagreeing after interruption **fail closed**;
  the system does not autonomously truncate, regenerate the
  checkpoint, or discard source evidence.
- Incomplete JSON, a deleted checkpoint, a missing journal,
  different policy, altered record, duplicate source,
  missing UTC day mark, reversed time or financial
  reconciliation failures all block subsequent writes.
  Correct recovery requires an audited external restore
  policy and should not fabricate P&L.
- Disk writes are fsynced before a confirmed append; a crash
  between the fsynced event and fsynced checkpoint may leave an
  **unavailable** journal, requiring operator review. No
  filesystem event journal can promise to recover from total
  device loss without redundant backups.
- The SHA-256 chain and checkpoint detect accidental changes,
  truncation and inconsistencies; they are **not authenticated
  exchange receipts** and do not protect against an adversary
  with access to rewrite both files. Independently authenticated
  upstream fill and settlement evidence is still required.
- The design intentionally replays all recorded events on
  each append. This bounds the first implementation's
  complexity and prioritizes auditability, but is **not**
  suitable for an ultra-low-latency hot loop without a
  measured, independently verified snapshot/index mechanism.

**Next dependency:** connect real replay/paper executions and
position/mark/funding attribution to these events, then test
partial fills, restart with open orders, margin claims, and
multi-position mark releases. The ledger must remain isolated
from exchange-order permissions until the risk engine and
source provenance checks are qualified.

## Filled-position bridge (new, awaiting external tests)

The next research-only component is `src/lab/position_reconcile.py`.
It converts a complete, time-ordered stream of **simulated linear
perpetual fills, full-position valuation snapshots and cash/funding
settlements** into reconciled economic events in
`lab.profit_journal`. It is not an exchange connection or
a qualified execution model.

- Use precise signed position quantities, average entry price,
  partial closes, flat exits, short-to-long/long-to-short reversals,
  separately supplied fill fees and *additional* slippage. Do
  not charge the same slippage both through the fill price and
  through an extra cash deduction.
- Every source fill generates two **atomically checkpointed**
  journal events: a realized trade cash-flow with its fee/cost
  reconciliation and a **complete, simultaneous revaluation**
  of all remaining open exposures. This avoids double counting
  previously marked unrealized profit, even when a positive
  mark on one position offsets a negative mark on another.
  The journal now supports `append_batch` and a verified
  `events()` history for this purpose.
- Historical source records must be **supplied in full** on
  every restart. Source IDs and derived journal events must
  match the entire stored prefix exactly; changed fills,
  missing original fills, duplicate source IDs, missing
  open-position marks and missing UTC day closes fail closed.
  No implicit backfill, order placement or auto-repair.
- A close-day event supplies the complete absolute UTC
  midnight valuation for all open positions, including
  zero-trade days. Funding/financing adjustments are
  separate auditable signed cash events.
- Scope is deliberately narrow: **linear contracts, one
  account/currency, contract multiplier=1**, explicit
  same-timestamp valuation of every remaining position.
  No inverse futures, spot financing, FX conversion,
  collateral/liquidation simulation or broker reconnection.
  Only *source-supplied* simulated fills are translated; the
  source is **not yet independently authenticated or
  durably archived** by this module.
- This is a first accounting adapter, not a hot-path runtime:
  the immutable source is replayed and the entire journal
  prefix checked on each reconciliation, then the remaining
  batches are committed under the journal lock. External
  acceptance tests and the existing full suite have not
  yet been reported.

**Further dependencies:** authenticate and durably persist
raw paper execution reports and market marks (including
partial fills and restarts); compare orders/positions with
the broker sandbox; reconcile cross-currency funding,
margin, liquidation, and venue minimum sizes; measure
CPU/latency overhead before any Rust production integration.
The Rust core and retired registered BTC research
strategies remain untouched.

## Economic objective and attribution

The primary objective is **net cumulative marked-equity growth**
after all costs, with constraints on worst-day loss, drawdown,
liquidation exposure, and prolonged unrecovered losses.
Secondary criteria are the distribution of positive/negative/flat
UTC days, active/idle days, profitability per unit risk,
profit factor when defined, and equity-recovery times.
The operator accepts very small positive days (even €0.20)
alongside occasional large net winners and material losses;
avoid optimizing **only** winning-day frequency.

For each UTC day, disclose:

- start/end marked total equity in a single reporting currency;
- realized trading P&L, changes in open-position P&L, fees,
  exchange funding, financing, slippage and FX-conversion charges;
- external cash deposits, withdrawals and transfers, kept
  separate from trading profits and from the return denominator;
- net trading P&L = end equity - start equity - net external flows,
  reconciled to the complete trade and inventory ledger;
- cumulative net trading result, running high-water mark,
  worst-day loss, drawdown, recovery duration, the magnitude of
  winning and losing days, and **all** zero-trade days;
- deployable capital, non-deployable profit reserve,
  unsettled balances and outstanding inventory/margin claims.

Do not sum daily percentages with changing capital bases
to infer the cumulative outcome. Do not sum EUR and USD returns
without timestamped, sourced FX conversion (including cost).
Never confuse a **profitable closed trade** with a **profitable
marked-to-market calendar day**.

## Reinvestment policy

Define a versioned, user-controlled `profit_reinvest_fraction`
with a value `f` between **0 and 1 inclusive**. It is **unset** until explicitly configured,
and must not silently default to 100% or to leverage growth.
Freeze the chosen fraction, conversion/rounding rule and effective
time before any research evaluation or paper session. Changes
produce explicit, versioned policy events; they do not rewrite
historical capital allocations.

After a *closed and fully reconciled* profitable trade or an
equivalent explicitly defined, completed settlement event has
a positive **realized net profit** `p` in the account currency:

    eligible_positive_profit = max(p, 0)
    future_deployable_credit = eligible_positive_profit * f
    retained_profit_credit  = eligible_positive_profit * (1 - f)

This **allocates** already earned equity into two bookkeeping
buckets. It does not create a second profit or cash inflow.
Positive open unrealized P&L is not reinvestable. If the exact
cost/funding or an associated position is unresolved, mark
the credit **pending**, not available for increased orders.
An exchange funding payment or fee must enter the economic
ledger **once**. If late corrections reduce realized profit,
reverse/reconcile the associated allocation explicitly.

For a negative net realization `p < 0`, **reduce trading capital
and total marked equity**. Never automatically increase stake
size to recover the loss. A retained-profit reserve should be
treated as *not deployable*; if it remains on the same cross-margin
venue, disclose that it may nonetheless be exposed to liquidation.
True asset isolation requires separate authorized accounts or
explicit withdrawals—neither is implied or performed automatically.

Open-position mark changes, margin requirements and liquidation
losses affect equity immediately even though they are not
reinvestable realized profit. The reallocation happens only
at an auditable event boundary (e.g. next risk evaluation),
never retroactively in the winning trade's fill calculation.

### Example (single currency; illustrative 50% reinvestment)

Assume starting *deployable* capital of **$1,000** and no retained
reserve, **one fully settled trading outcome per day**, and no
external flows. All examples are **net** of fees and funding.
The 50% fraction is an illustration, **not the selected setting**.

| Day | Daily net realized P&L | Added to deployable (50%) | Added to reserve (50%) | End deployable | End reserve | End total equity |
|---|---:|---:|---:|---:|---:|---:|
| Start | — | — | — | $1,000.00 | $0.00 | $1,000.00 |
| 1 | +$0.20 | +$0.10 | +$0.10 | $1,000.10 | $0.10 | $1,000.20 |
| 2 | +$120.00 | +$60.00 | +$60.00 | $1,060.10 | $60.10 | $1,120.20 |
| 3 | −$80.00 | $0.00 | $0.00 | $980.10 | $60.10 | $1,040.20 |

The outcome is **+$40.20 cumulative net profit**, despite the
$80 losing day. The $60.10 retained reserve is **part of**
the $1,040.20 equity, not an additional payout.
For multiple trade closures per day, apply the exact
per-event allocation policy to *each* eligible realized
positive net result; the sum of same-day profits and losses,
open marks and costs is still the one authoritative daily
net P&L. A negative daily equity change is never erased by
an earlier profitable trade.

## Trading size and risk isolation

Fractional profit allocation **must not** implicitly authorize
leverage growth. The maximum order size is the minimum of:

1. available, settled **deployable** risk capacity after
   retained reserve and outstanding orders;
2. a fixed precommitted cap on *absolute* account exposure
   and the venue's instrument minimums/limits;
3. a separately approved risk-per-trade and daily loss
   cap expressed against current **marked equity**;
4. margin/collateral, funding, liquidity and
   venue/portfolio concentration constraints.

Losses reduce risk capacity. Unrealized gains do not raise it.
If caps or minimum order sizes prevent deploying a few cents,
the credit remains as capital, not a forced new trade.
Never use doubling-down, martingale sizing, or larger leverage
solely to recover an $80 losing day.

## Required implementation and regression checks

Build this as an independently testable accounting/policy module
**before** connecting it to Rust order execution or the existing
Python market-data studies. Use exact decimal monetary arithmetic
with explicit quantization/currency and immutable event IDs:

1. **Conservation:** bucket transfers leave total equity unchanged;
   only actual trading returns, costs, marks and external flows
   move total equity. Prevent double-counting and timestamp
   reordering at session/restart boundaries.
2. **Daily reconciliation:** equity-change net P&L matches
   per-instrument realized results, open-position marks, funding,
   fees, financing and FX effects; deposits/withdrawals appear
   only as external flows. Calendar day close is UTC and a
   crossing position is marked once on each side.
3. **Reinvestment:** f=0, f=1 and a fractional example;
   losses, zero results, pending fills, fee corrections and
   settled funding never create spurious positive credits.
4. **Risk:** retained credits do not increase margin available
   for trading by assumption; concentration, exposure, daily
   loss and liquidation caps hold for changing capital, even
   on repeated wins; a loss lowers next sizing allowance.
5. **Evidence:** independent source/code/runtime, registered
   strategy/sizing-policy identity, out-of-sample period,
   full failed-trial history and reliable zero-trade days.
6. **Attribution:** compare a frozen fixed-capital policy with
   the frozen partial-reinvestment policy under **the same
   time-ordered signals/fills**, showing whether reported
   additional return is merely a higher average position size
   and whether drawdown or risk of ruin worsened.

**Exit criterion:** audited positive cumulative equity over
independent testing, feasible fill/fee/latency robustness, and
drawdown limits must jointly pass qualified, preregistered
evidence gates. Reinvesting part of profits alone does not
create a trading edge, a guaranteed profitable day, or
a guaranteed future recovery from losses.
