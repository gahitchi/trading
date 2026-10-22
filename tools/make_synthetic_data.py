#!/usr/bin/env python3
"""
Generate synthetic OHLCV candles (geometric Brownian motion) in Freqtrade's
feather format.

Why: this is the NULL-HYPOTHESIS CONTROL. A random walk has no exploitable
pattern by construction, so any strategy that shows profit on it is showing
you luck (or a bug), not an edge. Run strategies across many seeds: the spread
of their results on pure noise tells you how big a "profit" must be on real
data before it means anything.

Usage:
    python tools/make_synthetic_data.py --seed 1
    python tools/make_synthetic_data.py --seed 2 --drift 0.3 --outdir user_data/data/synthetic_s2

Then backtest with:  --datadir user_data/data/synthetic
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

# pair -> (start price, annualized volatility)
PAIRS = {
    "BTC/USDT": (60_000.0, 0.60),
    "ETH/USDT": (3_000.0, 0.75),
}
HOURS_PER_YEAR = 24 * 365
SUBSTEPS = 12  # 5-minute sub-steps per hourly candle, for realistic highs/lows


def make_pair(rng, start, vol, drift, n_candles):
    dt = 1.0 / (HOURS_PER_YEAR * SUBSTEPS)
    n = n_candles * SUBSTEPS
    # GBM log-returns: (mu - sigma^2/2) dt + sigma sqrt(dt) Z
    log_ret = (drift - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * rng.standard_normal(n)
    path = start * np.exp(np.cumsum(log_ret))
    path = np.concatenate([[start], path])

    steps = path[1:].reshape(n_candles, SUBSTEPS)
    opens = path[:-1].reshape(n_candles, SUBSTEPS)[:, 0]
    closes = steps[:, -1]
    highs = np.maximum(steps.max(axis=1), opens)
    lows = np.minimum(steps.min(axis=1), opens)
    # Volume loosely tied to the candle's range, plus noise.
    rng_pct = (highs - lows) / opens
    volume = rng.lognormal(mean=6.0, sigma=0.5, size=n_candles) * (1 + 50 * rng_pct)
    return opens, highs, lows, closes, volume


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--drift", type=float, default=0.0, help="annualized drift; 0 = pure random walk")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--outdir", default="user_data/data/synthetic")
    args = p.parse_args()

    rng = np.random.default_rng(args.seed)
    n = args.days * 24
    dates = pd.date_range(args.start, periods=n, freq="1h", tz="UTC")
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)

    for pair, (start, vol) in PAIRS.items():
        o, h, l, c, v = make_pair(rng, start, vol, args.drift, n)
        df = pd.DataFrame({"date": dates, "open": o, "high": h, "low": l, "close": c, "volume": v})
        path = out / f"{pair.replace('/', '_')}-1h.feather"
        df.to_feather(path, compression_level=9, compression="lz4")
        print(f"{pair}: {n} candles, {c[0]:.2f} -> {c[-1]:.2f} ({(c[-1] / o[0] - 1) * 100:+.1f}%)  -> {path}")


if __name__ == "__main__":
    main()
