# Roadmap

Where trading-lab is going, what is done, and what has to be true before each next step. Updated 2026-10-29.

## Goal

An automated, independent trading system with institutional-grade research and risk discipline that can trade
any asset class through the right broker or exchange. Fast (low-latency) trading comes first; longer-horizon
trading with an AI strategist comes later.

Crypto is the test bed because its markets never close, so every module can be run and tested at any hour.

## Constraints during testing

These hold until paper trading shows a profit, and they shape the order of the work:

- **No money spent.** Free public data only (exchange websockets and REST history), no paid feeds, no VPS.
  Interactive Brokers is not a fit for now: its minute data is behind a paywall.
- **Paper only.** Nothing places exchange orders or uses API keys until Gabriel decides otherwise (see
  `AGENTS.md`).
- **The machine is not always on.** Recording and paper trading run on a home PC that is switched off at
  times. Studies must tolerate gaps in the recordings and short sessions; nothing may assume a continuous
  24/7 history.
- **Home latency.** From a home connection, orders reach Binance or Bybit in tens to hundreds of milliseconds.
  Research must price that in (`--latency-ms`): an edge that only exists at co-located microsecond speed is not
  one we can trade yet.

## Principles

- **Platform:** NautilusTrader (Rust core, the same strategy code in backtest, sandbox and live). Strategies are
  Python first; Rust only for a hot path that a measurement shows is too slow.
- **No look-ahead.** Every study has a forward holdout that research never sees, tested once per finished
  configuration. Every configuration tried goes into the trial log, and results carry a deflated Sharpe ratio.
- **Costs first.** A signal counts only where its expected move beats fees, spread, slippage and latency.
- **One risk path.** Every strategy trades through the shared pre-trade risk manager and the kill switch.
  Neither is ever weakened to make a strategy work.
- **Green CI is not an edge.** A passing test suite proves the code does what it says, not that it makes money.

## Status

| Marker | Meaning |
|---|---|
| ✅ | Done and on `main` |
| 🔄 | In progress, or built and waiting for data |
| ⏳ | Next |
| 💤 | Later; not started |

## Phase 0: Foundation ✅

The lab that every later phase stands on.

| | Item | Where |
|---|---|---|
| ✅ | Market-data recorder for Binance USD-M (full book diffs) and Bybit linear (top 50 every 20 ms): books, trades, mark prices, funding | `lab record`, `docker compose` |
| ✅ | Read-only monitor: recorder health, feed liveness, coverage holes, disk runway, paper P&L and risk panel | `lab monitor` |
| ✅ | Pre-trade risk manager shared by all strategies: daily loss cap (persisted), notional and order limits, audit log | `lab.risk_manager` |
| ✅ | Kill switch with optional flatten; Telegram alerts | `lab kill`, `lab.notify` |
| ✅ | Forward holdout, holdout ledger, trial log, deflated Sharpe | `lab.fast_research`, `lab.carry_study` |
| ✅ | Backtest on recorded L2 books and live paper node (sandbox fills, no keys) | `lab fast backtest / paper` |
| ✅ | CI on Python 3.13 and 3.14, hash-locked Docker build | `.github/workflows/ci.yml` |

## Phase 1: Find an edge 🔄 (now)

Each line of research ends with a verdict, even when the verdict is "no edge".

| | Line of research | State |
|---|---|---|
| ✅ | **Taker signals on the majors** (`lab micro-study`): order-flow imbalance, book imbalance, microprice, trade flow, Hawkes intensity, absorption | Verdict: **no taker edge** on BTC, ETH, SOL. The predicted moves at 1–30 s are smaller than a taker round trip. |
| 🔄 | **Passive market making** (`lab survey`, `lab maker-study`, `PassiveMaker`) | Strategy built. Waiting for the maker study on recorded data: do passive fills earn more than the adverse moves that follow them, at home latency? |
| 🔄 | **Funding carry** (`lab carry study`) | Majors, 2023–2026: cash-and-carry earns about **5.5%/yr**, roughly the risk-free rate, so it is cash-like rather than an edge. Cross-venue funding loses after fees. Next: the pre-registered altcoin run (top 10, 20 M USDT/day turnover, 5 bps slippage), where funding is highest. |
| ⏳ | **Cross-venue lead-lag** (`micro-study --leader`, Binance vs Bybit) | Feature exists; not yet studied on real recordings of both venues. |

**Exit gate:** one strategy whose research result has a deflated Sharpe of at least 0.95 and that then passes its
one-time holdout test after all costs and the modelled latency. If none passes, the next candidates come from the
same cost-first screen (other venues' fee tiers, wider-spread perpetuals from `lab survey`, longer horizons).

## Phase 2: Paper trading ⏳

Run the Phase 1 survivor on live data with simulated fills, in the sessions the PC is on.

| | Item |
|---|---|
| ⏳ | Paper runner for the winning strategy (the fast and maker strategies have one; carry needs one that acts at the 00, 08 and 16 UTC funding times) |
| ⏳ | Paper P&L that carries across restarts, so many short sessions add up to one track record |
| ⏳ | Paper vs backtest comparison on the same hours: fills, slippage and P&L should match what research expected |
| ⏳ | Operations under real conditions: reconnects, exchange maintenance, kill switch and daily-loss stop exercised |

**Exit gate (the funding gate):** a paper track record that is profitable net of fees, with a t-statistic on the
mean trade of at least 2, over enough trades that the result is not luck, and consistent with the backtest.
Funding, and every spend below, is decided by Gabriel only after this.

## Phase 3: Small live trading 💤

Only after the funding gate and Gabriel's explicit decision.

- Real execution path with exchange API keys kept out of the repo; start with the smallest sizes the venue allows.
- Order, fill and position reconciliation against the exchange; restart recovery from the exchange's own state.
- An always-on host near the matching engine (for Binance, the AWS Tokyo region), and measured latency end to end.
- Profiling of the hot path; port to Rust only what the measurements show is the bottleneck.
- Risk limits sized to real capital, and alerts that reach a person.

## Phase 4: More venues and asset classes 💤

The final system trades any market through the right broker. NautilusTrader's adapters make venues pluggable;
the work is in each venue's data, fees, sessions and margin rules.

- More crypto venues (each one a recorder, fee model and paper path, like Bybit was).
- Then other asset classes (futures, forex, equities) through brokers whose data and costs fit the budget at
  that point. Each new market starts with the same steps: record or source data, study costs, paper trade.
- Trading calendars and session boundaries for markets that close, which crypto never needed.

## Phase 5: AI strategist 💤

Longer-horizon trading where an AI proposes trades or strategy settings.

- The AI writes typed, reviewable configurations (instrument, direction, size, horizon, limits), never orders.
- Every proposal goes through the same holdout, trial log and risk manager as human research.
- A person confirms any change to risk limits or markets; the AI cannot raise its own limits.

## Not now

- Paid data, a VPS or co-location, before the funding gate.
- Rewriting working Python in Rust without a measured bottleneck.
- Any real-order code path without Gabriel's decision.

## Updating this file

Change a marker only with evidence on `main`: the PR, the command that was run, and its result. A study verdict
goes in the Phase 1 table with the numbers behind it, including a negative one.
