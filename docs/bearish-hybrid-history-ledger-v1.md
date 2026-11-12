# Bearish hybrid: digest-pinned historical adapter and virtual daily ledger (v1)

**Research-only engineering milestone.** No actual historical market data or protected holdout has been accessed. These modules do not place trades, prove an exchange originated a bundle, qualify a strategy, or produce actual profit. They extend the separate draft replay PR #38, which depends on signal candidate PR #37.

## `lab.bearish_hybrid_history.load_hybrid_archive`

Input: exact JSON **bytes**, their independently pinned lowercase SHA-256, a protected UTC-midnight nanosecond boundary, and optionally an *externally audited* `funding_verified=True`. The default for funding verification is **false**. Do not compute the expected digest from untrusted bytes and call that provenance: pin it from an independently checked archival acquisition record. The caller must register any candidate/trial before examining trading outcomes.

Strict schema v1: `{version: 1, venue: "binance-usdm", symbol, bars, oi, funding, market}`.

- `bars`: ascending unique UTC minute ends; fields `end_ns, open, high, low, close, quote_volume, taker_buy_quote, source_complete`; positive internally consistent OHLC, nonnegative quote volumes and explicit source-quality booleans; **no filled/interpolated gaps**.
- `oi`: ascending publication records `{measured_ns, published_ns, value}`. Measurement cannot postdate publication; published samples are joined by availability time only, with an earlier sample at least 5 minutes and at most 60 minutes older. Missing valid snapshots result in `oi=None`, not invented open interest.
- `funding`: `{settlement_ns, published_ns, rate}` ordered by settlement and publication. Observed settlement and publication must both precede the decision bar; only 72-hour rolling events are supplied to the detector. **The supplied history is not automatically certified complete**. Unverified funding deliberately blocks a trade candidate.
- `market`: `{asof_ns, spot_listed, usdm_listed, mean_daily_turnover_usdt_30d}` in public-availability order. No backfilling of not-yet-known listings/turnover; the detector independently enforces its one-minute freshness and liquidity filters.

The adapter rejects extra or duplicate JSON keys, malformed types, nonfinite numbers, unaligned/duplicated timestamps, invalid OHLC, bad byte digests and **any protected-boundary or later timestamp anywhere in the bundle**. It has no downloader, secrets, network access, venue API integration or paper/live order endpoints. Data authenticity, symbol eligibility, historical liquidity, exchange-clock alignment, original recorder hashes, completeness and actual publish times need independently auditable external evidence; a textual `venue` label or SHA-256 cannot establish those facts.

## `lab.bearish_hybrid_ledger.daily_hybrid_virtual_ledger`

Pass the **same** complete pre-boundary observations, resulting `HybridReplay`, virtual starting account, risk parameters (if nondefault), costs and immutable boundary. The ledger requires all full UTC days (`00:01` through the next `00:00` in continuous minute ends), explicit trusted price flags and closed, fully resolved replay inventory. It **refuses** to value missing or partial market days or an unfinished/unknown position.

It independently reconciles modeled spread, slippage, fees and adverse funding reserve for each reference trade, checks entry/exit candle-open/close prices, stop-first ambiguous-candle ordering, time exits, overlap, projected risk/notional budget and intrabar worst-high insolvency. Completed trades settle into **hypothetical realized cash** at exit; while inventory is open, each daily UTC midnight reports marked equity using a conservative synthetic buy-to-cover price and fee/funding reserve. Flat days remain in the daily series. Exposes daily net P&L, returns, realized cash, end virtual equity, daily drawdown and counts of positive/negative/flat days.

**Limits:** every replay position size was calculated using the **frozen virtual-account snapshot**, so this first ledger evolves account cash but does **not** reinvest gains into later sizes; `position_sizing_compounds` is always `False`. There is no real exchange margin bracket, ADL, liquidation engine, actual mark price, subminute latency, queue/partial fills, authenticated funding cashflows, portfolio of multiple coins, or proof of economic edge. This is not the established `daily_short_equity` ledger's separately defined allocation model; do not combine their returns as interchangeable portfolios. If replay inventory is unresolved or the virtual account is $0, **no positive virtual-equity claim is allowed**. Actual capital and actual notional remain $0.

## Qualification steps still required

1. Pre-register the exact frozen parameters, symbols, external collection protocol, as-of clocks, data quality checks and research-family trial accounting; do **not** re-use the protected BTC 2026-05-08 boundary or pretend previously inspected BTC periods are out of sample.
2. Collect and independently verify archival Binance USD-M minute klines, contemporaneous published OI, settlement/publication timestamps, spot/perp listing snapshots and historically correct trailing turnover. Pin the raw evidence SHA-256, enforce the protected cutoff before assembly.
3. Replay distinct out-of-sample market periods. Quantify no-signal days, actual signal counts, daily returns, downside/positive-day distributions and negative controls, with correct uncertainty and multiplicity.
4. Add real as-of execution microstructure, dynamic virtual sizing from *realized* equity, documented compounding fraction, venue tier margin, actual funding and paper wallet receipts before any paper acceptance. Do not silently promote these research proxies to an executable trading strategy.

## Validation

Synthetic-only tests cover archive hash/duplicate-key rejection, future publication exclusion, pre-holdout gates, funding verification and missing bars; daily-ledger full/flat days, realized cash, overnight MTM, entry/exit reconciliation, stop-first correctness, insolvency, virtual notional caps and unresolved-exposure rejection. Run:

```bash
PYTHONPATH=src:. python -m pytest tests/test_bearish_hybrid_history.py tests/test_bearish_hybrid_ledger.py -q
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest -q
```
