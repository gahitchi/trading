# Bearish ensemble v1 — research-only proposal (2026-11-10)

**State: code and synthetic tests only. No historical profitability evidence, no protected-holdout use, no paper-trading or live-order authorization.**

The source `src/lab/bearish_ensemble.py` emits only `FLAT` or `SHORT_CANDIDATE`. It does not place an order, forecast profit, or change any existing failed strategy. It consumes *completed* UTC one-minute OHLC plus quote-volume and taker-buy quote-volume from one instrument, in end-timestamp order, and an explicit *estimated* full round-trip friction budget. Every sequence gap invalidates indicator warmup, and out-of-order or malformed data raises an error. A short candidate requires all five gates:

1. **Regime:** 20-minute EMA below 60-minute EMA, price below fast EMA.
2. **Impulse:** five-minute price momentum <= -25 basis points.
3. **Structure:** break below the prior 30-minute low, or the prior five-minute low for continuation, by two basis points. Current candle never enters the reference low.
4. **Participation:** quote-volume at least 1.5 times the preceding 60-minute median; the current minute's taker-selling share at least 58%; bearish close.
5. **Cost/risk:** 30-minute prior-bar ATR determines a 1.5x-ATR illustrative stop and 2:1 illustrative gross target; a trade candidate is vetoed if the target is under 2x modeled costs, estimated total friction exceeds 35 bps, stop exceeds 180 bps, or distance below fast EMA exceeds 3x ATR. One candidate at most per 30-minute cooldown; an open position or unready external risk governor vetoes all entries.

The EMA, momentum, breakdown, volume and selling measures are **correlated features**, not independent proof. The 2:1 target is a hypothetical price level, **not** expected profit; this module does not simulate its fills. Thresholds are **untested hypotheses**, not optimized settings. Do not infer that filtering the previous losing strategy makes it profitable. Underlying bars omit queue position, bid-ask spread and actual order-execution latency. `RoundTripCosts` is an estimate, not observed trade accounting. External margin, funding, inventory, sizing, liquidation and kill-switch constraints remain mandatory.

## Qualification sequence (still blocked)

- Treat v1 as a **new trial family**. Before examining historical outcomes, register the exact settings, source identity, symbols and time windows with `lab.research_family` / `lab.trials`; preserve all existing negative BTC attempts and the 2026-05-08 UTC protected BTC holdout.
- Build an offline signal adapter that maps `crash_study.bars` / verified archives to `MinuteBar`, stops at the registered research boundary *before opening files*, and records every skipped bar/quality veto. Do not mistake historical generated signals for filled trades.
- Implement an independently audited short execution replay using next available prices, 50/150/300/1,000-ms latency, actual or conservative spread, fees, funding, slippage, order rejects, partial fills and gap-through stops. Compare an exposure-matched short-only baseline and separately evaluate trade frequency, daily net P&L (including flat days), tail loss and drawdown. Explicitly reject negative-net or statistically unqualified trials; no parameter selection on the same holdout.
- Only a promising, *frozen* candidate may use its protected holdout once, after registration and an operator-approved qualification protocol. A separate, supervisor-controlled paper runner would then pass every prospective entry through the existing risk manager, never the standalone research signal directly.

## Local/Termux acceptance

On the branch `chatgpt/codex-maintenance`, in the existing Python virtual environment:

```sh
cd ~/trading-journal-validation
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
source ~/trading-venv/bin/activate
PYTHONPATH=src:. python -m pytest tests/test_bearish_ensemble.py -q
```

The 11 no-network regression tests cover bearish entry, upward/weak-selling vetoes, estimated-cost veto, risk position and risk-governor checks, missing-minute warmup reset, malformed/out-of-order input, prefix invariance, signal cooldown, extension veto, and prohibition on long candidates.

This is an **algorithm prototype**, not an evidence-backed claim that any strategy has an edge or can produce daily profit.


## Offline signal census adapter (2026-11-10)

`src/lab/bearish_ensemble_screen.py` exposes `screen_research_frame` as an **in-memory, read-only diagnostic**. It accepts preloaded closed minute bars and an explicit UTC-midnight pre-holdout boundary. It refuses any observation at or after that boundary, rejects duplicate/out-of-order timestamps, quarantines invalid candles and `oi_invalid` quality markers, and leaves source frames unchanged. It reports candidate timestamps, setups, warmup/gap/quality veto counts, and valid-vs-discarded candle counts. Unit tests use **synthetic data only**. No backtest fills, funding, equity, production controls, trial registration, public broker credentials, or real trades are involved.

A future guarded CLI should load research-only candles using `crash_data.load(root, "klines", symbol, before=boundary)` and build the full source-quality frame with the existing `crash_study.bars`. The cutoff must apply **before any parquet file is opened**, and the results must be trial-registered before historical outcomes are examined. This adapter deliberately does not select a boundary or open files for the caller. **Do not interpret its signals as trades, imply a performance edge, or run it on the protected BTC holdout.**


## Conservative short-side OHLC replay (2026-11-10)

`src/lab/bearish_ensemble_replay.py` now consumes the protected, read-only signal census with original signal stop/target distances. It is **not** a production execution model and does not read historical files, register experiments, open any holdout or place orders. A proposed short is referenced at the open of the candle **after one complete minute of entry delay** (the second subsequent candle). Signal overlap with an open modeled position is skipped. The entry/exit reference prices are adjusted for half-spread on each side, adverse slippage and fees on both executed notionals, and a configurable adverse funding reserve. All costs are reconciled to the original candle-open reference notional; the reserve is **not** evidence of actual funding settlement.

Each short exits at its stop, target or time limit (15 candles by default). For intrabar ambiguity, **stop wins**; adverse gap-through exits pay the worse candle-open price, while favorable gaps earn no better than the predetermined target. Any invalid/missing candle or OI-quality quarantine during an open position causes an **unresolved inventory** result and halts the replay. A quality failure already published in the fully completed candle before entry can veto the entry. A quality failure in the entry candle itself is **not** available at entry opening: it forces unresolved inventory and stops the replay rather than retrospectively canceling the trade. An absent entry candle reference is not assigned a fictional fill. No portfolio marked equity, daily return, lending/borrow costs, liquidation, partial fills, actual bid/ask quotes, queue position, order rejection, real latency or capital compounding is inferred.

This is a **synthetic unit-test target** only until a separate research-family member is registered under the existing trial machinery and the archived input sources are fingerprinted. Do not compare observed historical returns, vary parameters against data, or claim an advantage over retired strategies before that work. Later L2/tick replay must validate the 50/150/300/1,000-ms latency scenarios before live/paper candidacy.

### Additional portable tests

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest tests/test_bearish_ensemble.py tests/test_bearish_ensemble_screen.py tests/test_bearish_ensemble_replay.py -q
```

These three files contain 32 synthetic cases. Passing means the **defined model rules** are consistent; it does not establish a net-profitable edge.


## Guarded BTC research-family harness (2026-11-10)

`lab.bearish_ensemble_research` now exposes two explicit commands:

```sh
# Metadata-only candidate proposal; no price/OI source files read.
python -m lab.bearish_ensemble_research --config lab.toml --plan-out /tmp/btc-bearish-ensemble-plan.json

# NEVER run the following before reviewing the existing registered
# research family and ensuring every predecessor experiment is accounted for:
# lab research register /tmp/btc-bearish-ensemble-plan.json
# python -m lab.bearish_ensemble_research --config lab.toml --evaluate
```

The evaluation path fails before any market read if the plan isn't registered, its exact candidate settings are missing, the runtime/source identity changed, or the existing BTC holdout boundary differs from **2026-05-08 00:00 UTC**. Once registered, it reserves a durable trial before reading any source and fingerprints the actual pre-holdout klines, OI and resulting screening frame. It validates OI provenance and bars, with missing OI coverage explicitly vetoed, and reports signal and exit counts, including dates with no completed exits.

**The output is deliberately NOT a marked-equity daily P&L series.** The replay cannot price intratrade inventory, independently authenticate funding, or measure queue position and execution latency. Raw net trade-return sums (if all inventory resolves) are denominated per trade and are **not cumulative portfolio returns**. If inventory cannot be accounted for at a data gap, there are no net trade-return summary fields and the result is unavailable. Statistical qualification and paper/live order authorization always remain false.

The BTC historical data and previous failures have already been inspected: any BTC result is **retrospective**, not a new independent discovery. New ETH/SOL candidate families require their own prospective boundaries and coverage checks. This stage adds software safeguards, not validated trading performance.

### New guarded-study tests

```sh
PYTHONPATH=src:. python -m pytest tests/test_bearish_ensemble_research.py -q
```

The 9 synthetic tests cover exact pre-registration planning, missing/mismatched registration, accepted guarded replay, recorded source-read failure, unavailable open inventory, cutoff mutation, exclusive plan publication, and the actual production minute-grid/open-interest adapter (with no archive I/O). Run the full suite only in an environment with the project's optional NautilusTrader dependency.
