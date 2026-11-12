# Bearish hybrid: causal dynamic virtual capital and profit reserve (v1)

**Status: synthetic-only research engineering. This is not historical evidence, a real account, exchange execution, qualified expectancy or a claim of daily returns.** The short-only detector and its original parameters are unchanged. This additive experimental module depends on the offline replay and digest-pinned archive milestones in draft PRs #37–39. Do not treat these stacked changes as merged or production-ready.

New module: `lab.bearish_hybrid_dynamic`, function `replay_hybrid_dynamic`. No file, API, network, broker-key or live-order access. Input is a caller-validated sequence of `HybridObservation`, the same as for the earlier pure hybrid replay, with an independently fixed UTC-midnight protected cutoff. Do not access the protected BTC holdout or tune parameters on old negative-trial periods. Historical results require preregistration and independently verifiable market-data/availability evidence before outcome inspection.

## Cash and allocation semantics

- **Actual capital and actual order notional remain $0.** Pass an explicitly *virtual funded* snapshot for hypothesis experiments; a $0 virtual account intentionally cannot produce a dynamic equity curve. To see $0 signals use `replay_hybrid_observations(..., account=VirtualAccount())` in the earlier milestone. Neither path creates cash.
- `ProfitPolicy(reinvest_positive_fraction=p)` is mandatory, with an explicit finite `p` in `[0,1]`; there is **no default allocation fraction**. Realized positive net virtual P&L is split into `p` times P&L in *trading cash* and `(1-p)` times P&L in a separately reported **internal virtual profit reserve**. It is **not** a bank withdrawal, funding credit or actual savings.
- Realized negative net virtual P&L is **fully debited** against trading cash; the reserved positive-profit balance is **never borrowed to justify more notional**. Total virtual equity includes both balances, so moving profit into reserve does not count twice as profit or as a losing day.
- The next short's cap uses only *previously realized* trading cash, the explicitly stated collateral share of that cash, the earlier risk envelope (per-trade and daily loss, maximum per-symbol and portfolio gross and illustrative margin), and the actual delayed opening reference. The initial `free_collateral/equity` ratio is kept constant as a conservative *research assumption*, not a broker balance reconciliation.
- A full-day mark-to-market ledger includes each UTC day and all no-trade days. Open shorts are valued at conservative buy-to-cover prices and worst-case adverse funding reserves; positions can cross midnight. The next day's risk-budget baseline uses the prior midnight trading **mark**, not later cash or unseen prices; transfers to reserve are excluded from the daily-loss calculation. Settled profits/losses affect later exposure, never earlier signal decisions.

## Causal, conservative execution proxy

1. Preflight rejects partial days, invalid OHLC/quote volume, noncontiguous minutes, untrusted `source_complete`, protected-or-later candles, inconsistent account state and unsupported delay assumptions **before reporting any daily P&L**. `funding_verified=True` is still only a caller assertion; exchange/source completeness is not inferred from it.
2. The unmodified `BearishHybrid` sees one **completed** minute at a time; zero future candles are used in its decision. A short candidate creates a *pending hypothetical intent*. At least one whole minute closes before the next opening reference may become an entry.
3. At that entry, original absolute stop/target geometry and cost-adjusted reward are rechecked. Size is the smaller of the signal-time upper bound and the current account's risk bound. Ineligible or underfunded pending intents are skipped; no retroactive fill occurs.
4. On every open-position candle, if both stop and target are touched, use the **stop first**. A gap over the stop worsens exit reference; a gap under the target never improves credited exit. Stop, target and time-out use the shared `_fill` cost kernel (spread, fees, slippage and a fixed adverse funding reserve). Positive P&L is only credited after exit. Intrabar modeled insolvency fails closed; *no exchange margin tiers, fill queue, partial fills or liquidation are represented*.
5. The account's per-day loss budget, free collateral, pending inventory gate and kill/flatten modes are passed to the existing risk engine on every new completed minute. No new real buy/long or order path is exposed.

## Daily output and validation

`DynamicHybridReport` returns one record per complete UTC day, net/flat/positive/negative-day counts, risk-model drawdown, modeled trades, trade notionals, equity-at-entry audit trail, ending trading cash, protected virtual reserve and total virtual equity. All returned values are *synthetic modeled estimates*. `qualified=False` and `actual_notional=0.0` remain explicit. This module does not silently use the earlier frozen-notional `daily_hybrid_virtual_ledger`; the two outputs represent **different virtual sizing policies**, so compare them only as separate experiments with predeclared assumptions.

Synthetic tests prove: later winners increase prospective notional when profits are reinvested, losers reduce it for every policy, `p=0` keeps winners out of trading equity, reserve transfers do not double count daily P&L, overnight marks reconcile, next-day loss budgeting resets, zero initial cash and no collateral cannot trade, the kill switch works, source gaps/holdouts fail closed, funding attestation blocks candidates, entry delay is preserved, stop-first and insolvency are conservative, and changing future prices cannot modify an earlier signal.

Run on the full repository:

```bash
PYTHONPATH=src:. python -m pytest tests/test_bearish_hybrid_dynamic.py -q
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest -q
```

## Strict remaining blockers

1. Independently verifiable Binance USD-M raw data acquisition and delivery-time records for candle close, OI and funding, historic symbol/listing eligibility, rolling 30-day volume, fees and maker/taker spread **before** backtesting; a pinned SHA-256 alone proves neither venue authenticity nor archival completeness.
2. Registration of this new sizing policy as a separate research configuration in the immutable trial family; no BTC protected holdout usage or data-mined choice of `p` from prior outcomes.
3. Microstructure quote/tick replay (50/150/300/1,000 ms scenarios), uncertain entries, partial fills, real margin tiers, mark-price and settled funding, actual/estimated adverse selections, negative-control comparisons, calibrated inference and independent held-out data.
4. Only if demonstrated net expectancy survives these tests should it proceed to controlled demo-wallet reconciliation. Real funds and realized daily earnings remain prohibited assumptions at this milestone.
