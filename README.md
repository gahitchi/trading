# trading-lab

A research setup for testing trading strategies scientifically, built on
[Freqtrade](https://www.freqtrade.io) (free, open source, GPLv3).

**Paper only.** Every strategy calls `lab_safety.assert_paper_only()` at
startup: backtests, hyperopt and dry-run (live prices, fake money) work; live
trading with real money refuses to start, even if `dry_run` is flipped.

## Layout

```
config.json                     base config: Binance spot, BTC/USDT + ETH/USDT, 1h, dry_run, 1000 USDT fake wallet
config.private.example.json     web UI secrets template (copy to config.private.json, gitignored)
user_data/strategies/
  lab_safety.py                 refuses live trading
  BuyAndHold.py                 control: the baseline every strategy must beat
  SmaCrossover.py               H1, trend following
  RsiMeanReversion.py           H2, mean reversion
tools/
  make_synthetic_data.py        random-walk markets (no patterns by construction)
  null_test.py                  runs all strategies on N random markets -> the noise floor
experiments/LOG.md              lab notebook: hypothesis, setup, result, conclusion
```

## Setup (Linux)

```bash
cd trading-lab
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools wheel
pip install freqtrade
freqtrade list-strategies -c config.json --userdir user_data   # should list the 3 strategies
```

If pip fails on your system's Python version, use Freqtrade's official Docker
image instead (see the Freqtrade docs, "Docker quickstart").

## Workflow

**1. Get real data** (public candles, no API key needed):

```bash
freqtrade download-data -c config.json --userdir user_data --timerange 20220101- --timeframes 1h
```

**2. Split time into three periods, and respect them:**

| Period   | Range                  | Used for                                   |
|----------|------------------------|--------------------------------------------|
| Train    | 2022-01-01 → 2024-12-31 | developing ideas, hyperopt                 |
| Test     | 2025-01-01 → 2025-12-31 | checking what survived training            |
| Holdout  | 2026-01-01 → today     | final verdict, run ONCE per hypothesis     |

Looking at test or holdout results and then going back to tweak the strategy
leaks information and makes the test worthless. That is how overfitting happens.

**3. Backtest against the baseline:**

```bash
freqtrade backtesting -c config.json --userdir user_data \
  --strategy-list BuyAndHold SmaCrossover RsiMeanReversion --timerange 20220101-20241231
```

**4. Optimize parameters on train only:**

```bash
freqtrade hyperopt -c config.json --userdir user_data --strategy SmaCrossover \
  --hyperopt-loss SharpeHyperOptLossDaily --spaces buy --timerange 20220101-20241231 -e 300
```

The best parameters are saved as `SmaCrossover.json` next to the strategy and
loaded automatically. Then rerun step 3 with `--timerange 20250101-20251231`.
Expect results to get worse on the test period; how much worse is the finding.

**5. Compare against the noise floor:**

```bash
python tools/null_test.py --seeds 20
```

**6. Paper trade** what survives (live prices, fake money):

```bash
cp config.private.example.json config.private.json   # then fill secrets: openssl rand -hex 32
freqtrade install-ui
freqtrade trade -c config.json -c config.private.json --userdir user_data --strategy SmaCrossover
```

Web dashboard at http://127.0.0.1:8080 (bound to localhost only).

## What a result needs before it counts

1. Beats `BuyAndHold` after fees on the **test** period, not just train.
2. Beats it by more than the spread `null_test.py` shows on random data.
3. Holds on both pairs, not one.
4. Survives the holdout, run once.
5. Survives weeks of dry-run, where slippage and real fills show up.

Most ideas fail somewhere in this list. Recording where and why is the science.
