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
