# Lab notebook

One entry per experiment. Write the hypothesis and setup BEFORE running it.

Template:

```
## E<n> — <title> (<date>)
Hypothesis:
Setup:        strategy, params, pairs, timerange, data
Prediction:   what result would support / refute it
Result:
Conclusion:
Next:
```

---

## E0 — Null test: the noise floor (2026-10-22)

**Hypothesis:** On random-walk markets (no exploitable pattern by construction),
neither SmaCrossover nor RsiMeanReversion has an edge over BuyAndHold.

**Setup:** 20 synthetic markets (GBM, zero drift, BTC vol 60%, ETH vol 75%),
1h candles, 2024-01-15 → 2025-12-30, 0.1% fee per side, default parameters,
1000 USDT wallet. `python tools/null_test.py --seeds 20`

**Prediction:** Average excess over BuyAndHold ≤ 0 (fees only hurt); wide spread.

**Result:**

| Strategy         | Mean    | Median  | Stdev  | Mean excess vs B&H | Seeds ahead of B&H |
|------------------|---------|---------|--------|--------------------|--------------------|
| BuyAndHold       | +40.0%  | −7.4%   | 138.8% | —                  | —                  |
| SmaCrossover     | −4.3%   | −11.7%  | 68.5%  | −44.3%             | 35%                |
| RsiMeanReversion | +3.6%   | −13.1%  | 68.4%  | −36.4%             | 50%                |

Single-run extremes on pure noise: RsiMeanReversion +222.1% (seed 9),
SmaCrossover +197.2% (seed 16).

**Conclusion:** Supported. No edge on random data, as expected. The key lesson
is the spread: a strategy can show +200% on a market with no patterns at all.
A single backtest result on real data is not evidence; it must clearly exceed
this noise floor (stdev ≈ 68% for these strategies over two years).

Side observations:
- BuyAndHold mean (+40%) vs median (−7%): random-walk returns are skewed; a few
  huge winners pull the mean up while the typical outcome is a loss
  (volatility drag).
- On seed 1 (a −69% crash), both strategies "beat" B&H only because they sat in
  cash during the crash. Beating B&H in one falling market is not skill.

**Next:** E1, the same three strategies on real Binance data, train period only.
