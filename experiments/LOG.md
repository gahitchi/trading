# Lab notebook

Your own experiments. (The AI swarm writes its log to `research/NOTEBOOK.md`.)
Write the hypothesis and prediction BEFORE running anything.

```
## E<n>: <title> (<date>)
Hypothesis:
Setup:        command, signals, parameters, stocks, period
Prediction:   what result would support / refute it
Result:
Conclusion:
Next:
```

---

## E0: Pipeline verification on real prices (2026-10-22)

**Purpose.** Check the whole system end to end on genuine market data before trusting it.

**Setup.** `examples/goog-offline`: real GOOG daily prices 2004-08-19 .. 2013-03-01 (unadjusted; no splits
in the window). Train 2005-07-01 .. 2009-12-31, test 2010–2011, holdout 2012-01-01 .. 2013-03-01.
$100,000, IBKR Pro Fixed commissions, next-open fills with one tick of slippage.

**Results (train).**

| Signal | Return | Sharpe | Max drawdown | Exposure | Fills |
|---|---|---|---|---|---|
| buy_and_hold (control) | +112.4% | 0.64 | 65% | 99% | 1 |
| sma_trend (50/200) | +112.4% | 0.81 | 40% | 68% | 5 |
| rsi_reversion (2, 10/70, 200) | +7.8% | 0.19 | 22% | 16% | 69 |

- `null-test sma_trend` (30 bootstrap samples): Sharpe edge +0.17 vs noise mean −0.20, sd 0.26 → **p = 0.129,
  indistinguishable from noise**, despite the much smaller drawdown.
- `holdout sma_trend`: Sharpe 0.24 vs buy-and-hold 0.83 (edge −0.59). The training "edge" did not carry over.
- Accounting check: the execution agent's cash ledger matched the simulator's account to the cent in every run.

**Research swarm, two real cycles (Claude Code, haiku).**
1. `vol_managed_exposure`: volatility-managed exposure. Marked *broken*: the sandbox wrongly rejected
   `super().__init__()` and `from __future__ import annotations` (fixed afterwards; the final code passes).
   The Critic caught a genuine bug: the code went fully invested on invalid data instead of flat.
2. `wk52_high_proximity`: hold while within 10% of the 52-week high (George & Hwang, 2004). **Passed training**
   (Sharpe edge +0.31, p = 0.048), then **failed the test period** (edge −1.42). A textbook false positive,
   caught by the out-of-sample gate.

**Conclusion.** The machinery works, and on one stock over a few years nothing survives. Next: real data for the
full universe (`lab data fetch`), where samples are larger and noise floors tighter.

---

## E1: Professional statistics on the same sample (2026-10-22)

**Setup.** `examples/goog-offline`, train 2005-07 .. 2009-12, same costs. New tools: trial registry +
Deflated Sharpe Ratio of the active return, probability of backtest overfitting (CSCV), walk-forward analysis.

**Results.**
- `sma_trend(50,200)`: Sharpe edge +0.17 but **DSR 0.25** (needs 0.95), consistent with the noise floor (p ≈ 0.13).
- `lab pbo sma_trend --grid fast=10,20,50 slow=100,150,200`: **PBO = 0.80**. The in-sample best configuration
  averaged an out-of-sample active Sharpe of −0.34: choosing SMA parameters on GOOG is fitting noise.
- `lab walk-forward` (2y in-sample → 1y out-of-sample): stitched out-of-sample active Sharpe +0.16 with a 95%
  interval of −1.01 .. +1.39: no evidence either way.
- Volatility targeting (`--target-vol 0.15`): buy-and-hold Sharpe 0.64 → 1.04, max drawdown 65% → 30%, consistent
  with the volatility-managed-portfolio literature (Moreira & Muir).

**Swarm.** `turn_of_month_window` (calendar effect) first failed because signals had no access to dates; after
adding `w.dates`, `lab research retry` re-implemented it and it **failed training** on its merits (edge −0.21,
p = 0.24). Retrying hypotheses that already met the data is refused.

**Conclusion.** The stack now distinguishes "looks good" from "is real". Next: survivorship-free data for a
broad universe, then the IBKR paper deployment (RUNBOOK.md).
