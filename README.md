# trading-lab

[![ci](https://github.com/gahitchi/trading/actions/workflows/ci.yml/badge.svg)](https://github.com/gahitchi/trading/actions/workflows/ci.yml)

A fast crypto trading lab **in development**, built on [NautilusTrader](https://nautilustrader.io) (Rust core,
event-driven, the same strategy code in backtests and live trading). Binance USD-M perpetuals are the test bed
because the market never closes; more venues and asset classes come later. **Paper only:** nothing here places
exchange orders or needs API keys.

The old US-stock / Interactive Brokers system was removed on 2026-10-28. It is kept in the
`archive/us-stock-ibkr` tag.

## Install

Python 3.13 or 3.14 (NautilusTrader 2.0 requires it), Linux or macOS, with [uv](https://docs.astral.sh/uv/)
(`pacman -S uv` on Arch).

```bash
git clone https://github.com/gahitchi/trading.git && cd trading
uv sync --frozen --extra dev
source .venv/bin/activate
pytest -n auto
```

## Commands

| Command | What it does |
|---|---|
| `lab record [--symbols ...] [--check-only] [--status]` | record crypto order books, trades, mark prices and funding (public data, no keys) |
| `lab monitor [--port 8766]` | read-only dashboard of the recorder's health and the recorded data |
| `lab micro-study <symbol> [--horizons] [--fee-bps]` | do order-book and trade-flow features predict moves beyond trading costs? |
| `lab fast backtest / paper <symbol>` | the first fast strategy on recorded data, or live data with simulated fills |
| `lab fast backtest <symbol> --holdout [--again REASON]`, `lab fast release <symbol> --reason` | the one-time holdout test; turning the holdout into research data |
| `lab kill [--flatten]`, `lab resume` | kill switch: stop all new orders now (`results/KILL`), and clear it |

## Recording, research and paper trading

Fast-trading research needs order-book history, which exchanges don't give away: it has to be recorded.
`lab record` connects to Binance USD-M futures public market data (no API keys, no execution client, no orders)
and stores full-depth order-book updates, trades, mark prices and funding rates for each perpetual
(default `BTCUSDT ETHUSDT SOLUSDT`) in a NautilusTrader Parquet catalog under `data/market/catalog/`, promoted
every hour and at stop. `data/market/heartbeat.json` shows counts and the last event per instrument, and lists
instruments silent for over a minute. A lost connection ends the process with an error; run it where it is
restarted:

```bash
lab record --check-only                              # validate without connecting
lab record --status                                  # what has been saved to the catalog so far
docker compose up -d recorder                        # 24/7, restarted after outages; LAB_RECORD_SYMBOLS overrides
```

Read it back with `ParquetDataCatalog("data/market/catalog").query_order_book_deltas(["BTCUSDT-PERP.BINANCE"])`.

`lab monitor` (or `docker compose up -d recorder monitor`) serves a read-only dashboard at
http://127.0.0.1:8766: an overall status with the checks behind it (heartbeat fresh, every feed live, data
promoted into the catalog within the hour, days of disk left at the current write rate), events per second per
instrument over the last 30 minutes, and per instrument the rows and bytes in the catalog, the recorded span, and
its coverage: holes of over a minute between order-book files, which is where the recorder was down. The same data
is JSON at `/api/status`; `/health` answers 503 while the recorder is down, for an external uptime probe. It reads
`data/market/` only and never connects to an exchange. A connection drop the adapter recovers from without
restarting leaves no hole between files, so it doesn't show as a gap.

`lab micro-study BTCUSDT` replays a recording in receive-time order and tests the fast-trading features in
`lab.micro` (multi-level order-flow imbalance, book imbalance and microprice, aggressor-signed volume buckets;
ported from the Rust research crate): rank correlation with forward mid returns at 1, 5 and 30 seconds, and the
mean forward move in each feature's top and bottom decile next to a taker round trip (2 x fee + median spread).
A feature is only worth a strategy where that move exceeds the cost. It places no orders and models no queue
position or latency.

`lab fast backtest BTCUSDT` replays a recording through NautilusTrader's matching engine (L2 book, queue
position, consumed liquidity, maker/taker fees, a fixed order latency) with the first fast strategy, an
imbalance taker: when the 5-level book imbalance passes `--threshold`, it takes the opposite touch with an
immediate-or-cancel limit order and exits the same way after `--hold` seconds. `lab fast paper BTCUSDT` runs the
same code on live Binance data with NautilusTrader's sandbox execution: simulated fills against the live book, no
exchange orders, no keys. Both trade through the pre-trade risk manager (`lab.risk_manager`, see Risk):
`--max-loss` is the daily loss cap (flatten, no new risk until 00:00 UTC), `--max-position` caps the position and
each order's notional (default `--capital`), and `lab kill` stops them. Paper keeps the risk state and an audit log
in `results/fast/`, so a restart doesn't clear a daily-loss stop. The strategy is a platform
test, not a validated edge. The backtest reports round trips net of costs and the t-statistic of their mean.

Recorded data has a forward holdout. The first `micro-study` or `fast backtest` on a symbol reserves everything
recorded after that moment (`results/fast_partition.json`); research only sees data before it, and the recorder
keeps filling the holdout with data nobody has looked at. `lab fast backtest <symbol> --holdout` tests a finished
configuration on it once; a repeat needs `--again REASON`, and every holdout test is logged
(`results/fast_holdout_ledger.jsonl`). `lab fast release <symbol> --reason ...` turns the holdout into research
data and starts a new one; the boundary never moves back. Every run is logged in `results/fast_trials.jsonl`, and
each report shows how many configurations have been tried on the research data: the more tried, the less a good
result means.
Binance is not available in every country; check that it is in yours.


## Risk

**Pre-trade risk** (`lab.risk_manager`): every strategy is a
`RiskManagedStrategy` sharing one `RiskManager` per trader, and `add_strategy` refuses any other. Each order
passes, across all strategies, the kill switch (re-read every second), a daily loss cap on equity including
unrealized PnL (persisted, so a restart neither forgets the loss nor clears the stop; flattens and allows only
reducing orders until the next UTC day), worst-case per-instrument and gross notional limits (position plus live
orders on the same side) and a live-order cap; then NautilusTrader's own RiskEngine (rate limits, notional per
order, precision, balance, reduce-only). Reducing orders always pass except during a kill-switch halt; anything
that cannot be valued is denied. Every state change and denial goes to an optional JSONL audit file.

## Layout

```
src/lab/record.py         Binance USD-M market-data recorder
src/lab/micro.py          order-book and trade-flow features (OFI, imbalance, microprice, flow toxicity)
src/lab/micro_study.py    do the features predict moves beyond costs?
src/lab/fast.py           ImbalanceTaker strategy, backtest on recorded books, paper node
src/lab/fast_research.py  forward holdout, holdout ledger, trial log
src/lab/monitor.py        read-only dashboard of the recorder and its data (assets in monitor_ui/)
src/lab/risk_manager.py   pre-trade risk shared by every strategy
src/lab/safety.py         kill switch
src/lab/notify.py         Telegram alerts
src/lab/config.py         lab.toml: data and results directories
```

## License

LGPL-3.0-or-later, the same license family as NautilusTrader. For research and education; not investment advice.
