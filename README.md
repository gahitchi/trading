# trading-lab

[![ci](https://github.com/gahitchi/trading/actions/workflows/ci.yml/badge.svg)](https://github.com/gahitchi/trading/actions/workflows/ci.yml)

A fast crypto trading lab **in development**, built on [NautilusTrader](https://nautilustrader.io) (Rust core,
event-driven, the same strategy code in backtests and live trading). Binance USD-M perpetuals are the test bed
because the market never closes; more venues and asset classes come later. **Paper only:** nothing here places
exchange orders or needs API keys.

The old US-stock / Interactive Brokers system was removed on 2026-10-28. It is kept in the
`archive/us-stock-ibkr` tag.

Where the project is going and what comes next: [ROADMAP.md](ROADMAP.md).

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
| `lab record [--venue binance\|bybit] [--symbols ...] [--check-only] [--status]` | record crypto order books, trades, mark prices and funding (public data, no keys) |
| `lab monitor [--port 8766]` | read-only dashboard of the recorder's health and the recorded data |
| `lab micro-study <symbol> [--venue] [--leader] [--horizons] [--fee-bps]` | do order-book and trade-flow features predict moves beyond trading costs? |
| `lab survey [--venue] [--maker-bps]` | which perpetuals quote a spread wider than a maker round trip? (live public quotes) |
| `lab maker-study <symbol> [--venue] [--latency-ms] [--maker-bps]` | what do passive fills earn after the moves that follow them? |
| `lab fast backtest / paper <symbol>` | the first fast strategy on recorded data, or live data with simulated fills |
| `lab fast backtest <symbol> --holdout [--again REASON]`, `lab fast release <symbol> --reason` | the one-time holdout test; turning the holdout into research data |
| `lab carry fetch [--symbols ... \| --all] [--since 2023-01-01]` | download or update hourly funding and closes (Binance spot and perpetual, Bybit perpetual) |
| `lab carry study [--mode cash\|cross] [--lookback-h] [--entry-apr] [--exit-apr] [--all --top K --min-turnover M]` | does funding carry pay after costs? (holdout-guarded, logged) |
| `lab carry study --holdout [--again REASON]`, `lab carry release --reason` | the one-time carry holdout test; turning it into research data |
| `lab kill [--flatten]`, `lab resume` | kill switch: stop all new orders now (`results/KILL`), and clear it |

## Recording, research and paper trading

Fast-trading research needs order-book history, which exchanges don't give away: it has to be recorded.
`lab record` connects to one exchange's public perpetual-futures market data (no API keys, no execution client,
no orders) and stores order-book updates, trades, mark prices and funding rates for each perpetual (default
`BTCUSDT ETHUSDT SOLUSDT`) in a NautilusTrader Parquet catalog under `data/market/catalog/`, promoted every hour
and at stop:

| `--venue` | Market | Instrument IDs | Order book |
|---|---|---|---|
| `binance` (default) | Binance USD-M | `BTCUSDT-PERP.BINANCE` | every change to the full book |
| `bybit` | Bybit linear (USDT) | `BTCUSDT-LINEAR.BYBIT` | top 50 levels, pushed every 20 ms |

Run one recorder per venue: each is its own process, so one exchange's outage or maintenance never interrupts
the other's recording, and both write into the same catalog (each instrument has its own files).
`data/market/heartbeats/<venue>.json` shows counts and the last event per instrument, and lists instruments
silent for over a minute. A lost connection ends the process with an error; run it where it is restarted. Stop
it with Ctrl+C or SIGTERM and give it time to save (compose allows 2 minutes). If a run is killed anyway, what it
had written but not saved stays in `data/market/stream/`; the venue's recorder moves it into the catalog when it
next starts, and prints what it recovered.


```bash
lab record --check-only                              # validate without connecting
lab record --venue bybit --check-only
lab record --status                                  # what has been saved to the catalog so far (all venues)
docker compose up -d                                 # both recorders and the dashboard, restarted after outages
```

`LAB_RECORD_SYMBOLS` and `LAB_RECORD_BYBIT_SYMBOLS` override the symbols. Read the data back with
`ParquetDataCatalog("data/market/catalog").query_order_book_deltas(["BTCUSDT-LINEAR.BYBIT"])`. `micro-study`
reads either venue (`--venue`); `fast` still runs on the Binance recordings only.

`lab monitor` (the `monitor` service) serves a read-only dashboard at
http://127.0.0.1:8766: an overall status with the checks behind it (each venue's heartbeat fresh, every feed live, data
promoted into the catalog within the hour, no stream files left by a run that was killed before saving, days of
disk left at the current write rate), events per second per instrument over the last 30 minutes, and per instrument the rows and bytes in the catalog, the recorded span, and
its coverage: holes of over a minute between order-book files, which is where the recorder was down. The same data
is JSON at `/api/status`; `/health` answers 503 while no recorder is running, for an external uptime probe. It reads
`data/market/` and `results/` only and never connects to an exchange. A connection drop the adapter recovers from without
restarting leaves no hole between files, so it doesn't show as a gap.

Its paper-trading and risk panel follows `lab fast paper`: the run's equity curve (one sample a minute, the last
24 hours), its P&L from before the first trade and net of fees, today's P&L against the daily loss cap, the open
position, round trips with the t-statistic of their mean, the risk manager's state (ACTIVE, REDUCING, HALTED),
its limits and denials, the latest risk events from `results/fast/risk-audit.jsonl`, and the kill switch. The
paper node writes `results/fast/paper.json` every 5 seconds; a report older than 30 seconds reads as stopped.
The simulated account starts again at `--capital` on every run, so the P&L is per run.

`lab micro-study BTCUSDT` replays a recording in receive-time order and tests the fast-trading features in
`lab.micro` (multi-level order-flow imbalance, book imbalance and microprice, aggressor-signed volume buckets,
Hawkes buy/sell trade intensity, absorption at the touch, and with `--leader` a Kalman lead-lag gap; ported from the
Rust research crate): rank correlation with forward mid returns at 1, 5 and 30 seconds, and the
mean forward move in each feature's top and bottom decile next to a taker round trip (2 x fee + median spread).
A feature is only worth a strategy where that move exceeds the cost. It places no orders and models no queue
position or latency. `--leader ETHUSDT` or `--leader BTCUSDT --leader-venue bybit` replays a second instrument's
book and measures how much of its moves the studied symbol has not yet followed; research then stops at whichever
of the two holdouts starts first.

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
Research backtests also log daily net equity returns, including unrealized P&L, and print an IID deflated-Sharpe
diagnostic over the instrument's logged fast research configurations. It is unavailable when trial histories lack
daily returns, recorded windows differ, fewer than two comparable configurations exist, or fewer than four days
support finite return moments. Holdout results are excluded. Trial correlation and serial dependence still need
qualification before the diagnostic can support a gate; a short run cannot establish an edge.

`--strategy maker` runs the passive counterpart (`lab.maker.PassiveMaker`) through the same backtest, paper node,
risk manager, holdout and trial log. It keeps one post-only quote of `--size` on each side, at the touch or
`--improve` ticks inside the spread when it is wide enough; cancels and requotes when the wanted price changes;
stops quoting a side once the position reaches `--max-inventory` (default 3 x size) or while the book imbalance leans
against that side beyond `--min-favour`; and exits inventory older than `--max-hold` seconds at the touch. It is
ready to run where `lab maker-study` finds fills that are worth more than their fee, not before.

On BTC, ETH and SOL the predictable moves are a few bps against a 10 bps taker round trip, so the maker side is the
next question. `lab survey` samples every USDT perpetual's best bid and ask (30 snapshots, 10 s apart) and lists those
whose median spread leaves room over a maker round trip, with their 24 h turnover and the `.env` line that records
them. `lab maker-study SOLUSDT` then replays a recording with one hypothetical resting order at each touch: placed at
the back of the queue, live after `--latency-ms`, filled by trades through its price or once the queue ahead has
traded, cancelled when the touch moves away (`--improve N`: N ticks inside a wide spread instead, first in a queue of
its own). It reports the half spread each fill captured and the maker-signed move
of the mid 1 to 60 s later, net of the maker fee, split by whether the book imbalance favoured the order. Cancels are
assumed to come from ahead in the queue, which flatters fill rates.

`micro-study` and `maker-study` split recordings wherever complete book updates are more than one second apart.
Returns and markouts into or across those holes are excluded, features warm up again after a restart, and maker
orders are discarded while the book is stale. Maker fills per hour use time within the recorded segments,
excluding downtime. The one-second gap policy is included in each study's configuration fingerprint.

Recorded data has a forward holdout. The first `micro-study` or `fast backtest` on a symbol reserves everything
recorded after that moment (`results/fast_partition.json`); research only sees data before it, and the recorder
keeps filling the holdout with data nobody has looked at. `lab fast backtest <symbol> --holdout` tests a finished
configuration on it once; a repeat needs `--again REASON`, and every holdout test is logged
(`results/fast_holdout_ledger.jsonl`). `lab fast release <symbol> --reason ...` turns the holdout into research
data and starts a new one; the boundary never moves back. Every run is logged in `results/fast_trials.jsonl`, and
each report shows how many configurations have been tried on the research data: the more tried, the less a good
result means.

## Funding carry

The microstructure studies found no edge after taker costs at seconds horizons, so the next question is a slower
one: perpetual funding. `lab carry fetch` downloads years of hourly closes and funding settlements from the public
REST APIs (Binance USD-M and spot, Bybit linear; no keys) into `data/carry/`, and later runs only add what is new.
The default basket is ten liquid USDT pairs (BTC ETH SOL XRP BNB DOGE ADA LINK AVAX LTC). They were picked today,
so the past is flattered by their survival.

`lab carry study` backtests a delta-neutral trade per symbol with equal notional on each leg: `--mode cash` is long
Binance spot and short the Binance perpetual while funding is positive, `--mode cross` shorts the perpetual on
whichever of Binance and Bybit pays more funding and buys it on the other. At every funding time (00, 08, 16 UTC)
it looks at the funding the trade would have earned over the last `--lookback-h` hours, annualized, opens above
`--entry-apr` and closes below `--exit-apr`. A new position first earns the next settlement, and every change pays
both legs' taker fees (5 bps perpetual, 10 bps spot) and `--slippage-bps`. It reports the portfolio's net return a
year split into funding, basis and costs, Sharpe, a Newey-West t-statistic (positions last days, so daily returns
are autocorrelated), drawdown, an always-in benchmark for `cash`, and each symbol's P&L. Not modelled: leg
rebalancing, liquidation of the short perpetual in a rally, spot borrow, volume-tier fees.

On the majors the cash trade earns about what cash does, so the next place to look is the whole market, where
the highest funding is on small altcoins. `lab carry fetch --all` downloads every pair that trades today as both a
Binance perpetual and a Binance spot pair (Binance only; this takes hours, and an interrupted run resumes where it
stopped). `lab carry study --all --top 10 --min-turnover 20` then lets the symbols compete for 10 equal slots: at each
funding time it closes positions whose funding fell below `--exit-apr` and fills free slots with the highest funding
above `--entry-apr` among perpetuals that traded at least 20 M USDT a day over the last 30 days, all known at that
time. Today's list misses pairs delisted since, which flatters the result; thin altcoins also cost more to trade
than the majors, so raise `--slippage-bps`.

Carry studies carry missing prices forward for at most three hours. If an open position cannot be valued beyond
that limit, the study stops with an error rather than reporting a return. Top-K positions can exit only when both
legs have usable prices; a held symbol whose price history ends before the portfolio's does not free its slot,
and the study stops with an error. Repair incomplete history before using the results.

Carry has its own time holdout: the first study reserves the last 180 days fetched by then and everything fetched
later (`results/carry_partition.json`). Research runs drop that data before computing anything; `--holdout` tests
a finished configuration on it once. Every run goes into the trial log, and research runs print a deflated Sharpe
ratio as an IID research diagnostic. Trial correlation and serial dependence need separate qualification before
that statistic can support a gate; its value alone does not establish an edge.

Binance and Bybit are not available in every country; check that they are in yours.


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
src/lab/record.py         market-data recorder (Binance USD-M, Bybit linear)
src/lab/micro.py          order-book and trade-flow features (OFI, imbalance, microprice, flow, Hawkes, lead-lag, absorption)
src/lab/micro_study.py    do the features predict moves beyond costs?
src/lab/survey.py         spreads vs maker fees across all perpetuals (live public quotes)
src/lab/maker_study.py    passive fills and their markouts on recorded data
src/lab/fast.py           ImbalanceTaker strategy, backtest on recorded books, paper node
src/lab/maker.py          PassiveMaker strategy (post-only quotes, inventory cap), run through lab.fast
src/lab/fast_research.py  forward holdout, holdout ledger, trial log
src/lab/carry_data.py     hourly funding and closes from public REST history (Binance, Bybit)
src/lab/carry_study.py    funding carry backtest after costs, time holdout, deflated Sharpe
src/lab/monitor.py        read-only dashboard of the recorder and its data (assets in monitor_ui/)
src/lab/risk_manager.py   pre-trade risk shared by every strategy
src/lab/safety.py         kill switch
src/lab/notify.py         Telegram alerts
src/lab/config.py         lab.toml: data and results directories
```

## License

LGPL-3.0-or-later, the same license family as NautilusTrader. For research and education; not investment advice.
