# Bearish hybrid — offline candle replay adapter, v1

**Status: synthetic-only engineering adapter; no historical data, broker connections, genuine order-book execution, qualified research trial, or demonstrated profitability.** It extends the frozen signal model in `docs/bearish-hybrid-zero-capital-v1.md` without changing its thresholds. New module: `lab.bearish_hybrid_replay`.

## Contract

- The caller supplies a sequence of `HybridObservation`s in strictly increasing UTC one-minute *end* nanoseconds and `boundary_ns` at a fixed UTC midnight. Any record at/after the protected boundary rejects the entire input. The adapter reads no files and makes no network calls. The source, as-of timestamps, OI and funding publication completeness remain externally authenticated obligations. No metadata flag is independently verified by this adapter.
- The detector receives completed bars and the provided OI, market-eligibility and funding-as-of observations. Missing/invalid candle provenance resets warmup while flat. Missing active-position evidence, or a minute gap during a virtual position, halts the replay as **unresolved** rather than fabricating a fill or profit.
- A positive virtual-account signal creates an **unfilled hypothetical intent**. After at least **one fully completed whole minute** of delay, the next candle's opening reference is eligible for a modeled short. The order is skipped if this price has crossed the original stop or target, or if the original stop/reward/cost geometry fails for the delayed entry. Position notional is capped again at entry by the original `paper_size` risk function. This is **not** subsecond timing evidence.
- Holding period is at most 15 completed minute candles by default. When an OHLC minute touches both stop and target, the replay assumes the **stop was hit first**. Stop exits through worse opening gaps; target exits do not receive favorable gap improvements. A time exit uses the last completed close. Spread, entry/exit slippage, fees and *adverse funding reserve* use the existing bearish replay accounting kernel, not observed exchange fills or actual published settlement cash.
- `HybridReplay.trades` are `ShortTrade` reference outcomes and `virtual_notionals` are suggestion caps. They do **not** constitute an audited portfolio ledger; `account` is a **frozen** external snapshot that does not reinvest modeled results. At actual capital $0, signals only appear in `unfunded_signal_times_ns`, with no virtual trade. No real orders exist anywhere in this module.

## Unresolved before economic claims

The adapter has no authenticated market-history loader, trial pre-registration, actual quote/L2 spread, adverse selection, latency distributions, queue-position/partial fills, order reject/cancel state, funding settlement attribution, margin brackets/liquidation paths, *dynamic* virtual-account reconciliation or daily marked-equity reporting. It cannot prove profitable daily trading. The dedicated protected BTC holdout remains untouched; all test candles are artificial and are used to validate causality, reject bad evidence, and preserve conservative stop-first ordering. Never treat those synthetic tests as an alpha backtest. Any performance study must separately pre-register a new research family and count every parameter trial.

## Validation

```
PYTHONPATH=src:. python -m pytest tests/test_bearish_hybrid.py tests/test_bearish_hybrid_replay.py -q
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest -q
```
