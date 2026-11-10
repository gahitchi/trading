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
