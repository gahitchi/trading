# Guarded mark collateral diagnostics (B4d)

B4c's historical mark candles now have an explicit consumer. The optional B4d diagnostic values held
perpetual quantities at historical hourly mark closes and checks an adverse intrahour price/funding-debit
envelope. It retains B4b's separate fixed leg budgets, explicit maintenance fractions and fully funded spot.
It is not a historical exchange margin engine, liquidation simulator or funded return calculation.

```sh
# Illustrative stress assumptions, NOT historical exchange margin parameters or a trading proposal.
lab carry study --symbols BTCUSDT --mode cash \
  --collateral-valuation mark-ohlc \
  --collateral-long 1.1 --collateral-short 0.2 \
  --maintenance-long 0 --maintenance-short 0.1
```

The default remains `--collateral-valuation trade-close`. Mark mode requires all four assumptions and the
required venues' [mark cache](carry-mark-history.md). Cash needs Binance perpetual marks only; cross needs
Binance and Bybit. Cash's spot market retains its traded-price valuation and full inventory funding. Reference
leg names remain tied to the same markets across cross-mode reversals. Neither option alters signals,
quantities, costs or the strategy's normalized unfunded P&L. Funding payments still use the individually
recorded venue rates and the existing hour-end TRADE-close value proxy, not exact funding-event marks.

## Boundary valuations and execution prices

For each leg at an hourly check, let P be its cumulative trade-valued marked P&L after fees/funding under the
existing ledger, q its signed quantity and N its signed notional at that check's traded close. At mark close M:

```
mark P&L = P + q*M - N
equity = initial local collateral + mark P&L
perpetual requirement = explicit maintenance fraction * abs(q*M)
headroom = equity - requirement
```

Before-decision checks use OLD quantities, the old position's current trade notional and current settlement
cash flows, before current trading costs. After-trade checks use NEW quantities and charge the existing
actual-traded-notional costs. Entry mark/trade differences affect marked equity immediately; an exit realizes
at its TRADED price and leaves no residual mark adjustment. A reversal first values the old position, then
values the new one. Spot gains and other symbols never replenish a perpetual's budget. Losses remain local
through flat intervals/re-entry; holdouts start with fresh flat quantities and stated initial budgets.

An hourly mark close is a boundary proxy from the preceding candle, not an exact synchronous mark observed at
the execution instant. No currency/lot rounding, price-impact or liquidation fill model is implied.

## Intrahour envelope, not an event reconstruction

For the hour ending at T, only quantities already held before T's decision are exposed to that hour's mark
range. A new entry at T cannot inherit a prior high/low or settlement; an exit/reversal cannot hide them.
Long perpetuals use the candle LOW and shorts its HIGH. With maintenance fraction m in [0, 1), these give
the adverse price for the scenario's headroom calculation.

Funding events can have opposite signs within one hour. The loader therefore retains positive and negative
rate sums separately. `funding_debit_long/short` contains each held quantity's nonpositive gross funding
cash flow, valued at the same trade-close proxy as ordinary funding. These are diagnostic components, not
additional deductions from strategy P&L. Netting offsetting payments cannot erase the gross debit stress.

Starting from before-decision mark P&L at T:

```
envelope P&L = boundary mark P&L - current net funding + current gross funding debits
               + old_quantity * (adverse_mark - mark_close)
envelope requirement = maintenance fraction * abs(old_quantity * adverse_mark)
```

This deliberately puts all current-hour proxy debits before the adverse mark and all credits afterwards.
It is a conservative budget envelope under those fixed cash-flow assumptions, not evidence that this order
occurred or was feasible. Mark OHLC describes [T-1h, T), while the funding bucket includes settlements in
(T-1h, T], including an exact endpoint payment. Moving such a payment before a prior extreme can be more
adverse than the observed chronology. The report consequently records a stress interval, never an invented
liquidation time, count, fill or loss. It does not combine different venues' extrema into a claimed concurrent
portfolio event. Spot has no invented intrahour mark path; its existing cash checks remain separate.

No transfers, cash replenishment, rescue orders, forced liquidation or post-breach execution are introduced.
Paths after exhaustion remain counterfactual. No boundary/envelope breach observed is not a solvency pass.

## Guarded inputs and failure behaviour

`carry study` reserves its durable trial (and a holdout reservation where applicable) before evaluating. The
strategy window/cutoff remains anchored to primary trade/funding/turnover series; auxiliary marks cannot
extend it. After generating that bounded flat-start position path, mark mode loads each required venue only
from the result's first timestamp through its last timestamp, inclusive. A one-nanosecond exclusive upper
bound preserves the final hourly row without admitting later marks.

The mark loader filters Arrow rows before converting/validating OHLC values. Rewriting future or held-out
values cannot alter research arithmetic, availability or hashes. Whole-file source identity, physical UTC
schema and structural checks still apply; corrupt shared metadata cannot be treated as trustworthy.
Holdout mark loading starts at its first evaluated timestamp, including that boundary candle's close proxy,
without evaluating an earlier held position or attributing its interval to a fresh entry.

Each bounded open/high/low/close series is hashed separately as
`SYMBOL/mark_long/<column>` or `SYMBOL/mark_short/<column>` in the input manifest. In-window mark changes
change provenance; appending/changing later values does not. The raw venue funding inputs already identify
the event signs needed for gross debits. Both automatic cash evaluations are independently reserved, counted
and persisted with the selected valuation/model. Prior attempts and failed statistical validation remain.

An exposed perpetual needs valid complete OHLC at each pre/post check: no interpolation, forward filling or
trade-price fallback. Missing marks while flat are allowed. A held path must have an uninterrupted hourly
grid. Invalid marks, missing identity, invalid gross debit evidence or nonfinite arithmetic fail evaluation.
Such a failure remains a counted attempt; a failed holdout is still consumed and retry requires `--again`.

## Reports and checks

The persisted `collateral_scenario` identifies valuation `mark-ohlc` and model
`separate-leg-mark-ohlc-collateral-scenario-v1`. Per-leg `first_breach`, `minimum_headroom` and
`ex_post_initial_collateral_boundary` refer to the two hourly B4b-style checks, now mark-valued for perpetuals.
The nested `interval_stress` reports exposed hours, first envelope breach's enclosing window, hypothetical
equity/requirement/headroom/P&L, minimum headroom and an ex-post initial budget boundary for the envelope.
No exposed intervals gives zero observed hours and no minimum, not a qualification. The per-leg
`ex_post_stress_collateral_boundary` is the maximum of boundary and interval requirements. These boundaries
use the whole evaluated path, including counterfactual continuation; they are not prospective sizing advice.
Equality can breach a perpetual threshold, so the boundary need not be an admissible deposit.

Hourly frames expose gross debit components and aligned mark OHLC. `collateral_path` and
`mark_interval_path` expose detailed checks; durable attempts retain summaries and bounded input identities.
The CLI distinguishes boundary breaches from hypothetical stress intervals. `qualified` is always false.

Thirty-six added cases cover independent cash/marked-inventory reconciliation, mark/trade entry/exit gaps,
intrahour highs/lows, old/new reversal quantities, gross offsetting funding, missing/invalid marks and identity,
flat gaps, overflow, unchanged orders/P&L, research/holdout limits, exact final-row inclusion, changed input
hashes, failed-holdout retention and actual CLI persistence in both counted cash runs. Full suite: 588 passed
on Python 3.13.16; lint/format passed. No empirical candidate study or live API qualification was performed;
the previous B4c smoke's access failures remain disclosed.

Historical maintenance tiers/deductions, initial-margin admission, close-fee reserves, account margin modes,
exact funding-event marks, liquidation execution/losses, transfers/financing, rebalancing and survivorship
remain open. B4d cannot qualify B4/B5, the failed A8b inference method or the Phase 1 exit gate. Phase 1
continues; Phase 2 has not started.
