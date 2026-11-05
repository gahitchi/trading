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
