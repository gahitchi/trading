# Carry leg ledger (B4a)

Collateral and liquidation calculations require explicit positions. The previous carry calculation summed
hourly percentage returns as if each leg's notional reset every hour, while charging costs only when the
direction changed. That implied free rebalancing and did not describe a held position. B4a establishes
quantity/cash-flow accounting before B4 models solvency. No candidate or real-data result was inspected to
choose this change, and no profitability or statistical threshold changes.

Three regressions failed before the fix: a leg moving 100 → 110 → 121 earned 20% instead of 21%; its exit
fee ignored the larger held notional; and funding cash flows ignored the changing value of the held short.

## Entry, holding and exit

For direction w in {-1, 0, +1}, reference-long market price L and reference-short market price S, a new
nonzero position opens one quote-currency unit of INITIAL notional on each leg:

```
q_long  =  w / L_entry
q_short = -w / S_entry
```

Cash mode permits only w=0 or +1 (spot long, perpetual short). Cross mode also permits -1, which reverses the
positions on the two reference markets. Quantities stay fixed while w is unchanged. Price movements alone
do not trade, resize or generate fees. An exit sets both quantities to zero. A reversal closes the old
quantities and opens new quantities at that hour's prices. Each new entry is normalized to one unit again;
this is an allocation-normalized P&L study, not a funded/self-financing account simulation. Applying an actual
budget also needs venue lot/precision and minimum-notional rules; these normalized quantities are not orders.

Equal entry NOTIONAL does not mean equal base quantity when the two entry prices differ. The model exposes
that residual base exposure rather than claiming exact delta neutrality. Matching base quantities or
rebalancing them would be a separate, counted strategy policy with its own costs. Neither is implemented here.

For each hour T, with quantities carried from the preceding hour:

```
basis_long(T)  = q_long(T-1)  * (L(T) - L(T-1))
basis_short(T) = q_short(T-1) * (S(T) - S(T-1))
turnover_leg  = abs(q_leg(T) - q_leg(T-1)) * price_leg(T)
cost_leg      = turnover_leg * (taker_fee_leg + slippage_bps) / 10_000
```

Cash uses the spot taker fee on its reference-long market and perpetual taker fee on the short. Cross uses
the perpetual fee on both markets, including when their directions reverse. Fee/slippage assumptions must
be finite and nonnegative. Closing fees therefore follow the actual marked quantity, not the original unit
notional. Slippage remains a proportional cost assumption, not an order-book fill simulation.

P&L is funding plus both marked price changes minus costs. Open terminal quantities and marked exposures
remain in the result; there is no forced terminal sale or invented exit fee. A fresh holdout evaluation opens
fresh quantities from flat and earns no preceding price change or settlement. Existing three-hour price-gap
and funding-query evidence rules remain. A direction change needs usable prices on both legs; unknown held
valuation or nonfinite arithmetic makes the evaluation unavailable.

## Funding is separate for both legs

The cache's individual venue funding series are retained, bounded before arithmetic and included in the input
manifest. A rate difference alone is insufficient once the two held notionals differ. With positive rates paid
by longs and received by shorts, the current estimate is:

```
funding_long(T)  = -q_long(T-1)  * L(T) * rate_long(T)
funding_short(T) = -q_short(T-1) * S(T) * rate_short(T)
```

The spot rate is zero. Cross mode values both venues individually. The existing causal event grouping and
dual-venue query coverage apply. The signal/ranking still uses the trailing rate difference; that signal is
not relabelled as the already-held quantities' cash flow. Changes in both venues' common rate can change
cash flows and the input digest even if the difference is unchanged.

**The price in this funding estimate is an hour-end close proxy, possibly carried forward within the existing
short-gap allowance. Exchanges use position value at the funding mark price.** The model does not have those
historical event marks yet. It cannot claim exact funding payments, solvency, liquidation counts or a B4 pass.
Hourly event/decision ordering also does not establish subhour fill or publication latency performance.
This limitation is printed by the CLI and saved as `hour-end-close-proxy-v1` in every configuration and report.

Primary formula references: [linear-contract P&L](https://www.bybit.com/en/help-center/article/Profit-Loss-calculations-USDT-Contract)
and [funding payment calculation](https://www.bybit.com/en/help-center/article/Funding-fee-calculation).
These formulas motivate quantity/value accounting; this implementation is not a reproduction of the venue's
margin or liquidation engine.

## Reporting and comparability

`lab carry study` saves `leg_ledger` inside each authoritative research attempt, including automatic comparisons.
It records the model `fixed-entry-notional-quantities-v1`, funding proxy, units, per-symbol terminal quantities
and signed notionals, peak gross/net notional, peak residual base quantity and per-leg price/funding/cost/
turnover totals. `qualified` is always false. The CLI prints the attempt file path, shows peak gross notional
in its symbol table, and labels the economics unqualified. Legacy/custom summary frames without identified
leg evidence cannot claim an available leg ledger.

Hourly study DataFrames expose those quantities and component cash flows for analysis. The attempt stores the
summary and existing daily normalized P&L series, not a new copy of every hourly frame or the market data.
The daily basis remains `daily-net-notional-v1`: P&L per initial per-leg notional allocation, not account-equity
returns. Every configuration additionally fingerprints the leg model and funding valuation; source identity
and raw venue funding manifests also change. Earlier attempts remain in history and cannot be quietly pooled
with the new economics. The frozen daily-v1 statistical method and its failed independent validation remain
unchanged and unqualified.

## Evidence and remaining B4 work

The software checks cover the three original regressions, an independently expressed cash-transaction and
marked-inventory reference in cash and cross modes, positive/negative funding directions, reversal fees,
price-unit rescaling, retained terminal positions, fresh holdout quantities, invalid data/settings, missing
legacy evidence and actual CLI persistence. Existing outage, input-provenance and holdout-boundary tests pass.
The holdout test now preserves ALL research inputs while rewriting held-out funding; changing research prices
would correctly change quantity-weighted funding and is not a valid no-look-ahead test.

Still unmodelled: event mark-price funding; collateral per venue/leg; initial/maintenance margin tiers and
liquidation losses; transfers and funding/cash financing; rebalancing policy/costs; partial fills; borrow and
volume-tier fees; and historical listing/delisting sensitivity. In particular, spot gains do not automatically
fund the short perpetual's margin. A simple division by a leverage factor cannot qualify returns on capital.
No B2/B3 empirical run, B4 solvency acceptance, A8b statistical qualification or holdout profitability gate
passes because this accounting prerequisite exists. Phase 1 remains active; Phase 2 has not started.
