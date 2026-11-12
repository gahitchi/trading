# Zero-capital bearish hybrid — independent research model v1

**Created 2026-11-12. Status: pure signal/sizing prototype + synthetic tests only. Not an authenticated exchange-data study, completed historical backtest, executable paper-trader, proven edge, recommendation to trade, or live execution authorization.**

Code: `src/lab/bearish_hybrid.py`; tests: `tests/test_bearish_hybrid.py`.

## Why this is a separate hypothesis

The code base already documents unsuccessful BTC trials. `docs/crash-postmortem.md` records the initial short-the-crash trade as **−106.3847 modeled net bps/trade across 32 trades**. `docs/bearish-failed-retest-result-v1.md` records **−47.0794 bps/trade across 15**. `docs/bearish-ensemble-v1-historical-result.md` reports **−20.5162 bps/trade across 857**. These are not comparable trial returns, nor proof all bearish trading fails. **Do not reuse or tune any of those frozen historical candidates.**

This v1 is a *different, untested* state machine: sell-off → pause / measurable counter-move → renewed short-side break. It combines the crash study's direction/participation/open-interest context with the carry study's actual, lagged funding settlements and liquid cross-listed universe, plus a more conservative short-only risk budget. Price, volume and OI are not independent causal proof of downside expectancy. The exact fixed candidate below must be treated as a new research trial, counted with the complete existing search history.

## Funding and capital policy

- **Real starting capital: $0.** No live trades and no real profit. The model has **no broker credentials, exchange order calls, automation, wallet integration or paid-service dependency**. `Decision.actual_notional` is always `0.0`.
- An all-zero `VirtualAccount` can still produce `UNFUNDED_SIGNAL` after all market and strategy gates pass. It records a qualifying hypothetical short setup, explicitly **without** even a virtual fill or investment.
- To study position sizes, supply a *separately labeled* virtual account, e.g. equity `10_000` USDT, start-of-day equity `10_000`, free virtual collateral `5_000`, risk-ready true. It may emit `RESEARCH_SHORT` with a bounded **suggested virtual notional**, not a fill, funded account, wallet balance or P&L.
- Do not attribute simulated earnings, carry funding, compounded balances or reimbursements to actual cash. A future profit-reinvestment policy requires an operator-selected realized-net-profit fraction and the existing audited `lab.profit_allocation`/journal components. There is no default reinvestment percentage.

## Inputs and universe

Every operation is a pure function of **completed** UTC minute-end OHLCV bars and public-as-of metadata. Inputs must be validated/fingerprinted by an upstream collector. One strategy object per symbol. Never pass a partly formed minute bar.

- USDT symbol must be listed on **both Binance spot and USD-M perpetual**. Its perpetual's *completed trailing 30-day* average daily traded quote value must be at least **$20 million**. `MarketEligibility` must have been observed within 1 minute. Market-listing and turnover provenance are caller obligations; supplying a number is not independent verification.
- `Bar`: open/high/low/close, quote volume, taker-buy quote volume and **closed-bar** minute timestamp. Gaps reset all price warmup and pending setups; duplicate, reversed and invalid bars raise.
- `OIObservation`: two public-as-of snapshots 5–60 minutes apart. The current observation must be at most 15 minutes old and no later than the completed decision bar. Values must be positive and finite. **The snapshot timestamps represent availability, not exchange measurement time.** In older archive formats allow for the appropriate release delay *before* constructing it; never timestamp future OI as known.
- `FundingEvent`: event settlement time, time it became public, and actual signed decimal funding rate. Events are strictly ordered. To compute trailing **72-hour realized annualized funding**, add only rates whose settlements and public arrival precede the decision, then multiply by `8760/72 × 100`. This handles 1/4/8-hour intervals or other actual schedules without inventing a constant 8-hour calendar. An upstream source-completeness and mark-settlement audit must justify `funding_verified=True`; the flag alone is not evidence.
- `Costs`: explicitly supplied per-side fees/slippage, full spread, and adverse funding exposure in basis points. These are an **estimated** friction budget; real fills, mark prices, funding liabilities and exchange discounts are not automatically inferred.

## Precommitted candidate state machine

All values below are **research hypotheses** and have not passed empirical selection or out-of-sample validation.

1. **Sell-off impulse:** after at least 76 complete consecutive minute bars, price falls at least **80 bps in 15 minutes**, aggregate quote volume in those 15 minutes is at least **1.8×** the 15-minute projection of the preceding 60-minute *median minute volume*, and the impulse's aggregate aggressor-sell share is at least **58%**. ATR uses 30 fully completed bars *before* the impulse, not the crash itself. Record the impulse's low and close.
2. **Pause / limited counter-move:** for the next 2–12 completed minutes, observe a **subsequently completed** bar reaching at least **0.25 ATR** above the impulse low, but reject if the post-impulse high exceeds **1.5 ATR** above that low. This prevents immediately shorting into the first crash print. The pullback must be established in a **previous** completed bar, not retrospectively inferred from the confirmation bar's high/low ordering.
3. **Fresh bearish continuation:** later (minimum 2 minutes after impulse) a completed minute closes at least **2 bps below the original impulse low**, closes below its own open, has aggressor sell share at least **60%**, and quote volume at least **1.3×** the 60-minute median baseline. Do not enter using this completed candle's prices; the displayed entry is just a *reference price* for research risk geometry.
4. **Interpret OI, do not mechanically equate it with shorts:** `build_up` for OI ≥ +0.2%, `liquidation_flush` for OI ≤ −1.0%, otherwise `neutral`. All are descriptive contextual regimes; neither an OI rise nor falling OI is itself an entry and falling OI never creates a directional long. No OI or stale OI means no eligible signal.
5. **Incorporate funding without assuming carry exists:** trailing funding ≥ **+10% annualized** raises short *priority* by 1; OI `build_up` also adds 1. The priority is **0–2, not a probability or calibrated return forecast**. Negative funding can still accompany a short candidate, but its adverse cost must be included in `Costs`; it is never counted as income in advance. Missing/unverified funding prevents a trade candidate.
6. **Stop/target feasibility:** stop-reference price is above the higher of impulse close and the post-impulse reaction high, plus **3 bps**. Target-reference price is `entry_reference − 2×(stop_reference − entry_reference)`. Reject if target nonpositive, stop distance over **180 bps**, estimated all-in round-trip cost over **35 bps**, or gross reference target under **2×** round-trip cost. These are reference levels, **not executed stop orders or guaranteed fills**. The 2:1 geometry is not evidence of positive expectancy.
7. **Cooldown and capital:** one signal per symbol per 30 completed minutes; no positive virtual notional when risk governor is halted, not ready, already exposed, or daily-loss-capped. Return only `FLAT`, `WATCH`, `UNFUNDED_SIGNAL` or `RESEARCH_SHORT`, never a `LONG`.

## Bounded *virtual* sizing (non-executable)

Using a virtual account, the notional bound is the minimum of:

```
0.25% of virtual equity / (stop reference fraction + round-trip friction fraction)
remaining 1.0% virtual daily loss budget / same worst-reference-loss fraction
30% of virtual equity per symbol
(100% of virtual equity − already open gross reference notional)
free virtual collateral / max(25% assumed initial margin,
                               20% rally stress + 10% illustrative maintenance + 5% reserve)
```

If a future instrument's minimum exchange notional exceeds the bound, no trade. Defaults deliberately avoid leveraging above portfolio virtual equity. **Illustrative 25% initial / 10% maintenance are not Binance's position-tier margin brackets.** Even a bounded reference stop cannot guarantee loss containment with gap-through fills, missing ticks, failed orders, funding shocks, ADL, isolated/cross collateral differences or liquidation. Actual exchange risk must be independently qualified and fail closed. Halt preserves state; flatten requires a separate future inventory-aware executor and **does not execute through this module**.

## Economic deployment structure

- **Primary sleeve: bearish short continuation.** The impulse/pause/breakdown decision uses funding as a cost and priority feature. Capital and risk may permit 0–N simultaneous portfolio exposures through an *external* supervisor; the current pure detector is limited to a conservative one-position virtual snapshot and does not orchestrate multiple symbols.
- **Secondary sleeve: traditional spot-long / perp-short funding carry,** retained as an **entirely separate optional research/paper strategy** under `lab.carry_study` / `lab.carry_paper`, with equal base-unit legs. Such a sleeve uses positive funding for potential earnings but can lose on basis, fees, collateral shortages, margin liquidation, financing and venue dislocations. It requires real spot purchasing power and perpetual collateral: **allocate zero real capital to it at a $0 start**. No combination of the two sleeves is a guaranteed hedge. Do **not** mix carry returns (per-leg notional normalization) into a directional short's capital-based daily equity or count them twice.
- **Future virtual portfolio allocator**, not yet implemented: when funded virtual accounts can be reconciled, permit only risk-qualified independent strategies, preserve collateral for spot/carry margin and short losses, and report full mark-to-market equity and daily net P&L including flat days. Before live deployment, calibrate a dynamic risk allocation through separate pre-registered multi-strategy portfolio experiments. Do not silently set sleeve weights based on retrospective winners.

## What this implementation does *not* do

No fee tier or exchange minimum lookup, historical download, API integration, authenticated mark/position-tier margin, liquidation, option-free funding forecast, executed fills, partial fills, short portfolio allocation, account ledger, 15-minute time-exit execution, cancellation acknowledgements, matched exposure baseline, holdout access, profit reinvestment, production Rust handoff or user-facing CLI. It supplies causal **candidate decisions and upper-bound virtual sizing only**. It cannot assert positive daily returns or any statistically significant edge.

## Required research/acceptance sequence

1. **Preserve historical negatives:** keep old failed attempts, immutable family generations and the BTC protected cutoff (`2026-05-08T00:00:00Z`) untouched. A new research-family registration is mandatory **before reading any candidate outcome**. Do not create a new false-independent BTC holdout or run this candidate on previously inspected BTC prices as if they were unseen.
2. **New data/experiment:** choose genuinely independent symbol/time windows and freeze these settings, complete data provenance, availability timestamps, market eligibility and fee assumptions. The current code is a pure unit-tested prototype; do not register a trial automatically on import.
3. **Offline stream/replay:** source-verifiable candles, OI publication times, funding events and L2/tick spreads; stress execution at 50/150/300/1,000 ms; use a subsequent executable quote for entry, not the signal close. Fill missing or uncertain history with *no trade / unresolved*, not invented prices. Implement stop-first ambiguity, gaps, partial fills, funding settlement and margin-path stress.
4. **Gates:** daily *net marked-equity* curve including no-trade days, positive expectancy after fees, funding and impact, negative control baselines, measured active-day frequency, dependent-statistics/inference calibration, parameter-trial multiplicity and one frozen protected holdout. **The repository documents an unqualified previous daily-inference procedure; repair and independently validate that infrastructure before claiming a statistical pass.**
5. **Only after evidence:** persistent demo wallet, exchange minimum checks, margin/liquidation tiers, paper fill receipts, independent stop/kill/flatten and restarts, audited realized-net profit allocation. A future, separate owner-authorized live release may then be considered with *funded* cash. Rust can host the event-time hot path, but Python remains acceptable for offline research and unit tests.

## Local reproducibility

```
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
# once integrated, check out the isolated proposal branch or review its PR
PYTHONPATH=src:. python -m pytest tests/test_bearish_hybrid.py -q
ruff check src/lab/bearish_hybrid.py tests/test_bearish_hybrid.py
ruff format --check src/lab/bearish_hybrid.py tests/test_bearish_hybrid.py
```

The authoring environment ran the isolated synthetic pytest suite, not the existing repository's whole integration suite. No historical performance test has been run. Before merging, run the complete project's `ruff check src tests`, `ruff format --check src tests` and pytest suite in a full checkout and review this independent research protocol.