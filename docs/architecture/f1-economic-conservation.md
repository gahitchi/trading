# F1.0 exact economic journal — isolated design candidate

**2026-10-27.** Branch: `chatgpt/f1-economic-conservation-20261027`.

**Status: DEVELOPMENT ONLY — F0 and F1 are unqualified.** Do not wire this
prototype into the existing ExecutionAgent, Nautilus Portfolio, paper broker,
order journal, risk reservations, live dashboards or financial settlement.

The first F1 slice builds a standard-library-only **signed double-entry
journal** with immutable ledger snapshots, transaction IDs, SHA-256 hash
chaining, FIFO signed long/short inventory lots and exact `Decimal`
arithmetic. Values from `float`, nonfinite values, duplicates, unbalanced
entries and amounts requiring implicit rounding are refused rather than
silently approximated. The only currency is USD; entry amounts have strict
scale and precision bounds. Monetary postings are signed debits/credits with
`sum(postings) == 0` for every cash or fill event.

Supported pure accounting event types:

| Event | Cash | Other effects |
| --- | --- | --- |
| Capital deposit/withdrawal | +/- | opposite external capital equity |
| Buy/sell fill | signed executed notional and commission | inventory at FIFO cost, realized P&L and fee expense |
| Dividend | + | offset dividend income |
| Financing/borrow fee | - | offset expense |
| Split | no change | transform lot quantities only, preserve cash and basis |

**No synthetic market trade may be constructed for dividends, splits,
financing, interest, funding or settlement.** A fractional cash-in-lieu
process and other corporate action types are **not yet implemented**.
Decimal divisions whose results do not terminate are refused until a
documented currency-quantization/remainder policy and broker settlement
path are independently reviewed. This is an explicit limitation, not
complete corporate-action support.

## F1 dependencies not yet implemented

1. Native NautilusTrader Portfolio/Cache vs. journal balanced-economics
   parity after the *same* native fill or classified settlement.
2. Durable, event-source sequence and restart/out-of-order/correction
   semantics in the existing execution store.
3. Broker-realistic fractional and rounding policies, currency-specific
   precision, fees, FX, funding, short rebate and carry.
4. Point-in-time corporate-action and dividend entitlements/reinvestment,
   borrow recall, share-lending and cash-in-lieu.
5. Independent approval, F0 exit, and adversarial reconciliation across
   exact executed quantities including partial fills and cancel/fill races.

**This module must not carry a qualification or authorization API.**
The next implementation step is to specify a typed native-account
observation and an independent reconciliation adapter, still offline
and unqualified, *before* connecting any actual broker.
