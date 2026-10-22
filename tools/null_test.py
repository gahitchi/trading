#!/usr/bin/env python3
"""
Null-hypothesis test: backtest the strategies on N independent random-walk
markets and look at the DISTRIBUTION of results.

Random walks contain no exploitable pattern, so:
  - a strategy's average excess return over buy-and-hold here is the
    "luck + fees" baseline;
  - a result on real data only means something if it clearly falls outside
    the spread you see here.

Usage:
    python tools/null_test.py --seeds 20
    python tools/null_test.py --seeds 20 --drift 0.3      # trending (bull) random walks
"""

import argparse
import csv
import shlex
import statistics
import subprocess
import sys
from pathlib import Path

from freqtrade.data.btanalysis import get_latest_backtest_filename, load_backtest_stats

ROOT = Path(__file__).resolve().parent.parent
STRATEGIES = ["BuyAndHold", "SmaCrossover", "RsiMeanReversion"]


def run(cmd):
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"Command failed: {' '.join(cmd)}\n{res.stderr[-2000:]}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seeds", type=int, default=10)
    p.add_argument("--drift", type=float, default=0.0)
    p.add_argument("--freqtrade-cmd", default="freqtrade", help="how to invoke freqtrade")
    p.add_argument("--out", default="user_data/null_test_results.csv")
    args = p.parse_args()

    ft = shlex.split(args.freqtrade_cmd)
    results_dir = ROOT / "user_data" / "backtest_results"
    rows = []

    for seed in range(1, args.seeds + 1):
        datadir = f"user_data/data/null/seed_{seed}"
        run([sys.executable, "tools/make_synthetic_data.py", "--seed", str(seed),
             "--drift", str(args.drift), "--outdir", datadir])
        run(ft + ["backtesting", "-c", "config.json", "--userdir", "user_data", "--datadir", datadir,
                  "--strategy-list", *STRATEGIES, "--timerange", "20240115-", "--export", "trades"])
        stats = load_backtest_stats(results_dir / get_latest_backtest_filename(results_dir))
        row = {"seed": seed}
        for s in STRATEGIES:
            row[s] = stats["strategy"][s]["profit_total"] * 100
        rows.append(row)
        print(f"seed {seed:>3}: " + "  ".join(f"{s} {row[s]:+7.1f}%" for s in STRATEGIES), flush=True)

    out = ROOT / args.out
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["seed", *STRATEGIES])
        w.writeheader()
        w.writerows(rows)

    print(f"\n{len(rows)} random-walk markets, drift={args.drift}  (results: {out})")
    print(f"{'strategy':<18}{'mean':>9}{'median':>9}{'stdev':>9}   excess vs BuyAndHold (mean, % of seeds ahead)")
    for s in STRATEGIES:
        vals = [r[s] for r in rows]
        excess = [r[s] - r["BuyAndHold"] for r in rows]
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        line = f"{s:<18}{statistics.mean(vals):>+8.1f}%{statistics.median(vals):>+8.1f}%{sd:>8.1f}%"
        if s != "BuyAndHold":
            ahead = sum(e > 0 for e in excess) / len(excess) * 100
            line += f"   {statistics.mean(excess):+7.1f}%, {ahead:.0f}%"
        print(line)


if __name__ == "__main__":
    main()
