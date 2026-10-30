# Carry collateral scenarios (B4b)

B4a established quantities and separate leg cash flows. B4b now checks whether an explicitly assumed, fixed
budget for each leg can cover the hypothetical path. A paired trade can have zero combined price P&L while
its short perpetual exhausts its own budget. A spot gain is not an automatic transfer to that budget.

This is a diagnostic, not a historical margin engine. There are no exchange-default maintenance rates,
liquidation counts/losses or account-capital returns. No candidate outcome was inspected to choose the model,
and no profitability or statistical acceptance threshold changes.

## Explicit assumptions

Supply all four flags together, or the report says the scenario is unavailable:

```sh
# Illustrative stress assumptions, NOT exchange margin parameters or a proposed trading configuration.
lab carry study --symbols BTCUSDT --mode cash \
  --collateral-long 1.1 --collateral-short 0.2 \
  --maintenance-long 0 --maintenance-short 0.1
```

`long` and `short` refer to the reference markets in the ledger, even after a cross-mode direction reversal:
cash long = Binance spot, cross long = Bybit perpetual, short = Binance perpetual. Budgets are quote-currency
units per symbol's initial one-unit notional per leg. They are fixed initial deposits, not leverage, repeated
top-ups, or percentages. Maintenance settings are fractions (0.1 means 10%, not 0.1 bps); they must be finite
and in [0, 1). Deposits must be finite and nonnegative. Cash mode requires maintenance-long=0 because its spot
inventory must be fully paid for; it cannot receive a maintenance discount or a fictitious margin loan.

Each symbol has its own pair of silos. There is no pooling between symbols, legs or venues, and no automatic
top-K portfolio scaling. The report must not be added to or divided into the portfolio's normalized P&L to
manufacture a return on capital. It is not an implementation of an exchange's cross-margin account.

## Balance checks and event order

For each leg, let C be the fixed deposit, B the cumulative marked price P&L, F the cumulative funding cash
flow, and K the cumulative fees/slippage. The scenario's marked equity is:

```
equity = C + B + F - K
perpetual requirement = explicit maintenance fraction * abs(quantity * hourly close)
spot requirement = full current value of the held spot inventory
headroom = equity - requirement
```

Spot headroom is the unspent cash after buying inventory. Perpetual equity includes unrealized P&L under the
explicit proxy assumption. The model treats each leg's existing cash flows locally; it never uses a gain on
the other leg to pay its losses. Closing or reversing preserves the same cash history; re-entry does not reset
equity or introduce a new deposit. Evaluation after a holdout boundary starts with fresh flat quantities and
the same stated initial budgets, without research-period losses or income.

Every hour has two checks, in this order:

1. **Before decision:** mark the previous quantities and settle their funding, retaining the previous
   inventory's requirement. Current trade fees have not yet been paid.
2. **After trade:** apply the planned entry/exit/reversal costs and the resulting quantities' requirement.

The before-decision check prevents a planned exit or reversal from hiding an already exhausted old position.
A perpetual with nonzero quantity breaches at headroom <= 0. Negative spot cash or a negative flat balance
also breaches; exactly zero unused spot cash or a zero flat balance does not. These are diagnostic threshold
comparisons using the ledger's floating-point units, not venue currency rounding rules.

Example: both legs open at 100, hold quantities +0.01 and -0.01, and rise to 130 without costs/funding. Combined
price P&L is zero. With deposits 1.1 spot and 0.2 perpetual, spot equity is 1.4 and inventory is worth 1.3. The
perpetual equity is -0.1 and, at the illustrative 10% fraction, its requirement is 0.13. A planned exit cannot
hide that before-decision shortfall. The observed-path initial budget boundary for that perpetual is 0.43.

## Reporting and evidence

Each durable trial result has `collateral_scenario`, containing the model
`separate-leg-close-collateral-scenario-v1`, market mode, four assumptions, limitations and per-symbol/leg:

- First breach timestamp, check stage, equity, requirement, headroom and cumulative leg P&L.
- Minimum headroom and terminal hypothetical equity.
- The **ex-post initial collateral boundary**, max(0, max(requirement - cumulative leg P&L)) over both stages.

The boundary uses the entire evaluated path, including any counterfactual continuation after a breach. It is
not a prospective sizing recommendation or signal input. Perpetual equality can breach, so the reported
boundary is not necessarily an admissible deposit: strictly positive headroom is needed while exposed.
There is no liquidation, rescue transfer, replenishment, forced flattening or post-breach execution model.
The underlying strategy path continues unchanged and is explicitly counterfactual after exhaustion. Its
normalized P&L statistics remain unfunded estimates. No observed breach on the proxy grid is a solvency pass.

`lab.carry_collateral.collateral_path` also exposes both checks' balances as a time/stage DataFrame. The CLI
prints first breaches, minimum headroom and ex-post boundaries; trial storage retains the summary, existing
daily series and bounded input provenance rather than another copy of the hourly path. All four assumptions
and the scenario model are fingerprinted before evaluation, including the automatically counted cash-mode
always-in comparison. Invalid/partial assumptions fail before boundary discovery; an invalid identified
ledger or nonfinite cumulative arithmetic fails the evaluation. Earlier attempts and negative evidence remain.
`available` only means the specified diagnostic was computed; **`qualified` is always false**.

Checks include an independently expressed cash-and-inventory account, both cross directions, entry/exit/
reversal ordering, fully funded spot, equality thresholds, funding-only exhaustion, retained losses across
re-entry, fresh holdout budgets, other-symbol isolation, price-unit rescaling, causal prefix invariance,
invalid inputs/identity, overflow and actual CLI publication in both counted cash evaluations.

## What still blocks B4 acceptance

Hourly trade closes (possibly carried for short data gaps) and funding close proxies cannot reconstruct
mark-driven intrahour liquidation. Actual initial-margin admission, historical maintenance tiers/deductions,
estimated close-fee reserves, account margin modes, collateral haircuts, mark-price funding and liquidation
execution/losses are missing. So are transfer latency/costs, cash financing, rebalancing policy, venue precision
and survivorship sensitivity. A funded strategy simulator must stop or explicitly model execution at a breach;
this diagnostic does neither and cannot qualify B4, B5 or the Phase 1 exit gate.

The subsequent [B4c mark-history downloader](carry-mark-history.md) stores separate hourly perpetual mark
OHLC as an input prerequisite. This B4b diagnostic still uses its declared trade-close proxy: mark ingestion
alone does not integrate causal mark valuation, recover exact funding-event marks or establish margin tiers.

Primary references for the distinction: Bybit's [maintenance margin description](https://www.bybit.com/en/help-center/article/Maintenance-Margin-USDT-Contract)
uses mark-based position value and tier deductions, and its [liquidation process](https://www.bybit.com/en/help-center/article/UTA-Trading-Rules-Liquidation-Process)
depends on account mode and liquidation procedures. Those venue mechanisms are not reproduced by a constant
fraction applied to hourly closes. Phase 1 remains active; Phase 2 has not started.
