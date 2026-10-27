# F1 whole-repository accounting audit — 2026-10-27

**Scope:** `chatgpt/f1-economic-conservation-20261027` (isolated prototype),
the inherited F0 branch, existing live/simulator accounting, storage, fee,
margin, borrow and test paths. **Decision:** F1 **NOT QUALIFIED**; this
document is an engineering gap register, not independent review.

F0 has a green exact-source GitHub acceptance *candidate*, but operator Arch
and private data custody and independent F0 acceptance are still pending.
F1 code may be developed concurrently in a separate owned branch; **nothing
on this branch receives F1/F6/F8 or broker/release authority**.

## F1 feature-by-feature status across the *entire software*

| F1 contract / evidence | Existing platform | New isolated F1 slice | Still missing to qualify |
| --- | --- | --- | --- |
| Balanced cash/inventory/income/expense postings | `ExecutionAgent` tracks cash, positions, commissions, financing with floats; no signed double-entry journal | `src/lab/economic_ledger.py` pure USD/Decimal balanced per-entry postings | Reviewed source-of-truth integration, journal vs Native Nautilus book reconciliation |
| Exact monetary quantities and fee semantics | `_apply_fill` casts `last_qty`, `last_px`, fee to `float`; notional is rounded via `math.floor(...*100+0.5)/100` | Rejects float/nonfinite inputs and implicitly rounded FIFO divisions; separate commission expense | A documented currency tick/minor-unit, commission, sub-cent, precision and rounding contract aligned to native `Money` and broker fills |
| Long, short and FIFO realized P&L | Existing positions are floats; no independently auditable FIFO entry inventory | Immutable signed long/short lots, partial closes, cross-through-zero, exact realized entry | Independently reviewed broker-native avg/FIFO accounting reconciliation, cost-basis conventions, reversals/corrections |
| Conservation & replay | Existing tests assert backtest `ledger_vs_account ~ 0`, calculated from execution cash less simulated reconstructed cash; no full general ledger | Every entry balances to zero; hash chain; full replay from business events checks exact positions and postings | An independently sourced native oracle, reference parity scenarios and externally anchored immutable audit identity |
| Fees, slippage and spread/impact | `LabFeeModel` applies IBKR-like commissions plus estimated spread/impact, returns a cent-rounded native `Money`; Python totals use floats | Positive `Decimal` commission debited separately | Fee attribution parity to native fills, source evidence, rebates, per-fill minimums, multi-piece partial fills and exact minor-unit rules |
| External capital/deposit/withdrawal | Allocation/dedicated account modes; dedicated account may accept unexplained broker cash deltas under a threshold | Explicit `CAPITAL_IN`/`CAPITAL_OUT` journal entries | Broker transaction IDs, classified settlement sources and native `Portfolio/Cache` updates; no automatic classification from a balance difference |
| Dividends and distributions | Market data may be adjusted; no F1 cash/position native settlement parity | Explicit non-trade dividend income posting | Ex-/pay-date entitlements, taxes/withholding, shorts' manufactured distributions, native account settlement and non-duplicate adjusted-price treatment |
| Financing and borrow | `margin.py` accrues assumed debit/credit APR and borrow fees using floats; `BorrowBook` can provide historical short-fee observations; `ExecutionAgent.financing` is a separate estimate | Explicit borrow-fee and financing debit postings | Native account cash parity (especially cash-credit/short rebates), provenance of financing rates, recognized vs estimated charges, no double-counted dedicated vs allocated accounts |
| Splits, reverse splits, corporate actions | `data.py` records split histories and `costs.py` normalizes per-share fee estimates; delisting simulation models exist | Share count adjusts without synthetic trade or cash; cost basis preserved | Native simulator share/lot transformation, fractional/cash-in-lieu policy, reverse/merger/spin-off, qualified delisting and source-as-of evidence |
| Durable restart/correction/economic dedup | `ExecutionStore` uses SQLite `BEGIN IMMEDIATE`, snapshots and keyed trade/order history, and has interruption regression tests | In-memory immutable deterministic journal only | Append-only normalized economic journal inside an atomic DB transaction with actual order-fill IDs, reconciliation after crash, corrected/voided events, tamper evidence, migration/backfill and restart parity |
| Broker/Nautilus Portfolio parity | `account_cash()` reconstructs margin cash from account total less position avg cost; `backtest.py` records the resulting float difference | Explicitly no engine, broker or risk wiring | A **classified native settlement path** for non-fills and tests on both native `Cache/Portfolio` and independent ledger; do not fake fills, modify only `ExecutionAgent.cash`, or inject invented prices/volume |
| Multi-currency and multiproduct | Architecture roadmap extends to equities, FX, crypto | USD cash / stock quantities only by design | Precision per currency, FX accounting, collateral, perpetual funding and derivative mark-to-market; require additional independently reviewed F1 cases before broadening scope |

### Existing defensible strengths to preserve

- The execution agent journals order intent before submission, attributes fills
  to broker trade IDs, handles pending/late fills and deduplicates them. These
  are *execution-lifecycle controls*, **not** exact economic conservation.
- The existing `ExecutionStore` atomically persists snapshot and indexed
  order/trade history under SQLite; interrupted projection is recoverable.
- Paper-only and kill-switch/reconcile guards are already present and must not
  be weakened or circumvented during integration.
- Backtest `ledger_vs_account` checks and the GOOG engine regressions are
  useful reference tests; they do not independently prove full economic
  accounting or native split/dividend settlement.

### Concrete material gaps and faults (priority order)

**F1-G01 / blocking — no live use of the exact journal.** The new F1
`EconomicLedger` is isolated and intentionally not fed real or simulated
native fill events; the production execution path still computes in binary
floats. Bridge only after audit of exact native `Money` and ordered fills.

**F1-G02 / blocking — nontrade native economic settlement is absent.**
Independent cash posting without changing the Nautilus account/portfolio would
invalidate `ledger_vs_account`. Treat dividends/interest/transfers/splits as
typed, independently verified native adjustments, **never** as synthetic market
trades at arbitrary prices or one-share book fixes. Fully test native ledger
states after every action.

**F1-G03 / blocking — precision policy differs.** The old runtime rounds
a computed float notional to cents; the isolated prototype refuses implicit
rounding. The final contract needs explicit broker-exact quantization, fees,
fractions, remainder custody and penny/subcent reconciliation.

**F1-G04 / blocking — external cash is not classified.** The dedicated mode
can book any small unexplained cash delta as `external_cash`. Such a delta
could conceal missing fills/fee settlements and cannot be evidence of the
underlying dividend, interest, transfer or tax transaction.

**F1-G05 / blocking — financing is not double-entry native-account parity.**
Borrow fees, broker debit rates and cash/short-credit accrue as model floats;
the simulator may never record an equivalent native account cash event. Distinguish
estimated research carry from actual, settled cash, by period, source and account.

**F1-G06 / blocking — corporate actions lack end-to-end conservation.**
Split metadata in fee calculations and adjusted historical prices are not a
position/basis/cash event stream. Non-fractional and fractional action types
must preserve share and economic value without executable fake print volume.

**F1-G07 / blocking — evidence/restart.** The new journal hash chain is not
externally anchored and does not survive process restart. Normalize and
atomically persist typed entries with native fills, reconciled snapshots and
idempotent correction/replay. The old transactional order store is only
supporting infrastructure, not an economic ledger.

**F1-G08 / blocking — parity test coverage.** Require independent, non-oracular
reference arithmetic over buy/partial sell/short cover/fees and broker-native
`Money` across cash/margin accounts, several partial fills, cancel/fill race,
settlement from ex-date, split and cash-in-lieu, financing over weekends,
recovery, rounding edges, replay tampering and every native-account action.

**F1-G09 / advisory — complexity bound.** The prototype's in-memory event
history is quadratic to audit/append and is limited to exact terminating
Decimal divisions; settle deterministic scale/remainder rules and streaming
validation before any full-history use. This is intentionally an oracle slice.

### Ordered F1 implementation / exit checklist

1. **F1.1 (under development):** immutable Decimal journal, signed FIFO
   inventory, documented share/cash precision and deterministic independent
   business-event replay. No production/broker wiring.
2. **F1.2:** typed observation adapters for native account/positions/fills
   and independent rounding/valuation oracle; explicit native settlement API
   review, without mock executable trades.
3. **F1.3:** classify financing, borrow, distributions, external cash,
   splits and cash-in-lieu, including expected-vs-settled semantics.
4. **F1.4:** durable atomic event-custody/restart and dedup, model corrections,
   unsupported adjustment refusal, migration isolation.
5. **F1.5:** adversarial parity matrix and independent reviewer validation of
   *every* economic event against both books, including partial fills and
   crash/restart boundaries.
6. **F1 exit:** F0 officially accepted; same exact F1 candidate passes CI
   and Arch/native parity suite; the independent reviewer and owner sign off
   on balances, native settle paths and remaining assumptions.

**Never use a green F1 unit test as evidence of live, paper-broker, or
release authorization.** F6/F8 remain separate downstream gates. This
report does not modify execution authority.
