# Roadmap

The step-by-step plan for trading-lab: what the software does today, what gets built next and in which order, and
the measured thresholds that decide whether the project moves on. Updated 2026-10-29.

This copy is maintained on `chatgpt/codex-maintenance`. Its implementation evidence is listed at the end;
branch work does not change the completion markers inherited from `main`, and no empirical gate has passed
merely because its supporting code is implemented.

## 1. Goal

An automated, independent trading system with institutional-grade research and risk discipline, able to trade
any asset class through the right broker or exchange. Fast (low-latency) trading comes first. Longer-horizon
trading with an AI strategist comes later.

Crypto perpetual futures are the test bed: the market never closes, the data is free, and every module can be
run and tested at any hour.

## 2. Constraints until paper trading is profitable

| Constraint | What it means for the software |
|---|---|
| No money spent | Free public data only (exchange websockets, `data.binance.vision`-style REST history). No paid feeds, no VPS, no co-location. Interactive Brokers is out for now: minute data is behind a paywall. |
| Paper only | Nothing places exchange orders or holds API keys until Gabriel decides otherwise (`AGENTS.md`). Execution is NautilusTrader's sandbox: live data, simulated fills. |
| The PC is not always on | Recordings have holes and paper runs are sessions of hours, not weeks. Every study must accept gappy data; every track record must add up across sessions. |
| Home latency | Execution studies and backtests are run at 50, 150 and 300 ms (`--latency-ms`). `micro-study` is a signal screen without execution and cannot qualify latency performance. An edge that only exists below 50 ms is noted and parked for Phase 4. |

## 3. How the software fits together today

```
 Binance USD-M ws ──┐                                    ┌─> lab micro-study   (taker signals)
 Bybit linear ws ───┼─> lab record ─> Parquet catalog ───┼─> lab maker-study   (passive fills)
                    │   (one process     data/market/    ├─> lab fast backtest (taker / maker strategy,
                    │    per venue)      catalog/        │    L2 matching engine, latency, fees)
                    │                                    └─> lab monitor       (health, coverage, disk)
 Binance/Bybit REST ──> lab carry fetch ─> data/carry/ ───> lab carry study   (funding carry)
 Binance/Bybit REST ──> lab survey (live spreads vs maker fees)

 Live ws ─> lab fast paper ─> strategy ─> RiskManager ─> NautilusTrader RiskEngine ─> sandbox fills
                                 │            │  kill switch (results/KILL), daily loss cap,
                                 │            │  notional / gross / order-count limits, audit JSONL
                                 └────────────┴─> results/fast/paper.json ─> lab monitor (P&L, risk panel)

 Research discipline (every study): forward holdout per instrument, holdout ledger, trial log
   results/fast_partition.json · fast_holdout_ledger.jsonl · fast_trials.jsonl · carry_partition.json
```

## 4. Where each line of research stands

| Line | Command | Verdict so far |
|---|---|---|
| Taker signals on BTC, ETH, SOL | `lab micro-study` | **No edge.** Order-flow imbalance, book imbalance, microprice, trade flow, Hawkes intensity and absorption predict moves at 1–30 s, but the top-decile move is smaller than a taker round trip (2 × 5 bps + spread). `ImbalanceTaker` stays as a platform test only. |
| Passive market making | `lab survey`, `lab maker-study`, `PassiveMaker` | **Waiting for data.** Strategy and study are built and tested; the maker study has not yet been run on enough recorded hours. |
| Funding carry, majors | `lab carry study --mode cash/cross` | **Cash-like.** 2023-01 to 2026-04, 10 majors: cash-and-carry +5.5%/yr (always-in +6.3%), about the risk-free rate. Cross-venue funding −0.4%/yr after fees. The 10% APR entry filter hurts. |
| Funding carry, altcoins | `lab carry study --all --top 10 --min-turnover 20` | **Pending.** Pre-registered first run: top 10 slots, 20 M USDT/day turnover, 5 bps slippage. |
| Cross-venue lead-lag | `lab micro-study --leader` | **Not studied.** Feature built; needs overlapping Binance and Bybit recordings. |

## 5. Phases

| Phase | Name | State | Exit gate (summary) |
|---|---|---|---|
| 0 | Foundation | ✅ done | — |
| 1 | Find an edge | 🔄 now | One strategy passes its holdout after costs at 150 ms |
| 2 | Paper trading | ⏳ next | ≥ 300 paper trades over ≥ 14 session-days, net profitable, t ≥ 2, matches backtest |
| 3 | Small live trading | 💤 | Funding decision by Gabriel; 4 weeks live within risk limits |
| 4 | Speed: co-location and Rust hot paths | 💤 | Measured latency budget met |
| 5 | More venues and asset classes | 💤 | Each new market repeats steps 1–2 |
| 6 | AI strategist (longer horizons) | 💤 | AI proposals pass the same gates as human research |

Markers: ✅ done on `main` · 🔄 in progress · ⏳ next · 💤 later.

---

## Phase 0: Foundation ✅

| Step | Built | Where |
|---|---|---|
| 0.1 | Recorder, Binance USD-M full book diffs + trades + mark price + funding | `lab.record`, `lab record` |
| 0.2 | Recorder, Bybit linear top 50 levels every 20 ms; one process per venue, both into one catalog | `lab record --venue bybit` |
| 0.3 | Crash recovery: stream files of a killed run promoted on next start; hourly promotion | `lab.record` |
| 0.4 | Docker compose: both recorders + monitor, restart after outages | `docker-compose.yml` |
| 0.5 | Monitor: heartbeats, feed liveness, coverage holes > 1 min, disk runway, `/api/status`, `/health` | `lab.monitor` |
| 0.6 | Microstructure features: OFI, imbalance, microprice, flow buckets, Hawkes, absorption, Kalman lead-lag | `lab.micro` |
| 0.7 | Forward holdout, holdout ledger, trial log, release with logged reason | `lab.fast_research` |
| 0.8 | L2 backtest (queue position, consumed liquidity, fees, latency) and sandbox paper node | `lab.fast` |
| 0.9 | Pre-trade risk manager shared by all strategies, fail-closed, persisted daily-loss stop | `lab.risk_manager` |
| 0.10 | Kill switch (halt / flatten), Telegram alerts | `lab.safety`, `lab.notify` |
| 0.11 | Funding history fetch and carry backtest with time holdout and deflated Sharpe | `lab.carry_data`, `lab.carry_study` |
| 0.12 | CI: ruff, pytest on Python 3.13 and 3.14, package and hash-locked Docker build | `.github/workflows/ci.yml` |

---

## Phase 1: Find an edge 🔄

Three tracks run in parallel because they need different data: the maker track needs recorded books, the carry
track needs only REST history, the lead-lag track needs both venues recorded at the same time.

### Track A: passive market making (main track)

The taker verdict says the predicted moves are too small to pay 10 bps. A maker pays about 2 × 2 bps and earns the
half spread, but gets filled most often just before the price moves against it (adverse selection). Track A
measures whether what is left is positive at home latency.

| Step | Do | Command / module | Done when |
|---|---|---|---|
| A1 | Find perpetuals whose spread leaves room over a maker round trip. Run at 3 different times of day (Asia, Europe, US hours) on both venues. | `lab survey --venue binance --samples 60`, same with `--venue bybit` | Up to 5 symbols chosen with median spread − 2 × maker fee ≥ 1 bp in all 3 runs and turnover ≥ 20 M USDT/day; list written in this file. |
| A2 | Record the A1 symbols next to BTC, ETH, SOL. | `LAB_RECORD_SYMBOLS`, `LAB_RECORD_BYBIT_SYMBOLS`, `docker compose up -d` | Monitor shows each symbol live. |
| A3 | Accumulate data. Holes are fine; the monitor's coverage table is the count. | `lab record --status`, `lab monitor` | ≥ 72 recorded hours per symbol before the research boundary, spread over ≥ 7 calendar days (mix of hours and weekdays). |
| A4 | Split studies at recorder holes; exclude stale or cross-hole targets, reset feature warmup and simulated resting orders, and measure fills per recorded hour. The implemented conservative boundary is > 1 s without a usable complete book; include it in the trial fingerprint. | `lab.micro_study`, `lab.maker_study`, `lab.study_time`, `tests/` | Synthetic 10-minute holes never create returns, markouts or stale fills; research resumes afterwards. Slow sampling alone is not a recorder hole. |
| A5 | Maker study grid per symbol: latency 50 / 150 / 300 ms × `--improve 0 / 1`. 6 configurations per symbol, all counted in the trial log. | `lab maker-study SYMBOL --latency-ms L --improve I` | Table of `net_bps`, `t`, `fills_per_h` per horizon and imbalance group, pasted into §4. |
| A6 | Research gate for a symbol (on research data only), after A6b qualifies the uncertainty estimate. | — | At 150 ms: `net_bps` > 0 at the 30 s and 60 s horizons with adjusted t ≥ 3, ≥ 10 fills/recorded hour; and the "neutral + in favour" groups positive on their own (the strategy can skip "against" with `--min-favour`). |
| A6b | Account for overlapping markouts and dependence before using a maker study's t-statistic as evidence. Pre-register a non-overlapping sampling or block/HAC procedure and its settings before examining the candidate's result. | `lab.maker_study`, `tests/` | Synthetic dependent samples cannot inflate the independent sample count; report effective observations and uncertainty. The current naive t is descriptive only. |
| A7 | Turn the best A6 configuration into strategy settings and backtest it with inventory, partial fills and the risk manager. | `lab fast backtest SYMBOL --strategy maker --improve I --max-inventory Q --max-hold S --latency-ms 150` | Net P&L per round trip > 0 after fees with t ≥ 2; max drawdown < 2% of `--capital`; result in the trial log. |
| A7b | `lab fast` backtests and paper-trades Binance only. If the A6 winner is a Bybit symbol, add Bybit to `lab fast` (instrument mapping, fee model, sandbox venue) first. | `lab.fast` | `lab fast backtest SYMBOL --venue bybit` runs on the Bybit recordings; tests cover it. |
| A7a | Validate the execution model on gappy recordings and at home latency before accepting A7. A4 protects descriptive studies, not the matching engine. | `lab.fast`, `lab.maker`, `tests/` | Explicit handling of outstanding orders, inventory and unknown valuation at disconnects; no free close or silent loss removal; submission and cancellation latency assumptions documented and stressed. |
| A8 | Add daily net equity returns (including fees and unrealized P&L) and a trial-adjusted Sharpe diagnostic to `lab fast backtest`. | `lab.fast`, `lab.fast_research`, `lab.cli` | Each research run logs the return series and prints a diagnostic or an explicit unavailable reason. Missing trial statistics, different data windows, or too few observations cannot silently produce a score. Tests cover it. |
| A8b | Qualify statistical inference for fast and carry research: use comparable return frequencies and data, account for serial dependence, and disclose the treatment of correlated trials. Preserve every attempted configuration, including unsuccessful ones. | `lab.fast_research`, `lab.carry_study`, `tests/` | The pre-registered method has numerical and synthetic dependence checks, data/code provenance, and a complete trial history. The current IID diagnostic is not by itself a gate pass or a probability that a strategy will make money. |
| A9a | Reserve holdout evaluations atomically before reading held-out data, against a frozen configuration and data boundary. Keep failed/interrupted attempts and require a reason for repeats. | `lab.fast_research`, `lab.cli`, `tests/` | Two concurrent processes cannot both claim the same evaluation; a crash cannot erase its use; choosing another configuration after seeing a result is recorded as holdout reuse. |
| A9 | One-time holdout test of the frozen A7 configuration. | `lab fast backtest SYMBOL --strategy maker ... --holdout` | Holdout P&L > 0 after fees, t ≥ 2, deflated Sharpe of the research result ≥ 0.95. A fail is written in §4 and the symbol is dropped; `lab fast release` only with a logged reason. |

Track A fails if no symbol passes A6. Then the maker studies move to the Bybit fee schedule, to wider-spread
symbols from a fresh survey, and to longer quote lifetimes. Each attempt is a counted trial.

### Track B: funding carry on altcoins

| Step | Do | Command / module | Done when |
|---|---|---|---|
| B1 | Download every Binance perpetual that also has a spot pair. Takes hours and resumes after interruption, so it suits a PC that is switched off. | `lab carry fetch --all` | Each pair covers its available history from the later of listing or 2023-01 through the latest closed hour; gaps and unavailable history are explicit, and transient failures retried. |
| B2 | The pre-registered run, exactly as registered. | `lab carry study --all --top 10 --min-turnover 20 --slippage-bps 5` | Result in §4: return/yr split into funding, basis and costs; Sharpe; Newey-West t; drawdown; deflated Sharpe. |
| B3 | Stress the costs: slippage 10 and 20 bps; turnover floor 50 M. Counted trials. | same, with `--slippage-bps`, `--min-turnover` | Return still > 10%/yr at 10 bps slippage (twice what the majors earn), otherwise the track stops. |
| B4 | Model what the backtest leaves out, in order of risk: liquidation of the short perpetual in a rally (margin needed per leg), leg drift and rebalancing, delisted pairs (survivorship). | `lab.carry_study` | Report collateral requirements, liquidation events and losses, rebalancing costs, and survivorship sensitivity separately. Liquidation is a solvency constraint, not just a fee line. Delisted history is included where it exists; unavailable data remain a limitation. |
| B5 | Holdout test of the frozen configuration, after A8b and A9a qualification. | `lab carry study ... --holdout` | Holdout return > 10%/yr after all costs, adjusted t ≥ 2, qualified deflated Sharpe ≥ 0.95. |

Carry suits the constraints well: decisions happen only at 00, 08 and 16 UTC, so a PC that is on for a few hours a
day can run it. It is a slow strategy, not a fast one; it earns its place as the first paper candidate if Track A
fails, and later as a cash-like base return under the fast strategies.

### Track C: cross-venue lead-lag

| Step | Do | Command / module | Done when |
|---|---|---|---|
| C1 | Record the same symbols on both venues at the same time (A2 does this). | `docker compose up -d` | ≥ 48 overlapping hours. |
| C2 | Check the lead-lag interval against the real update rate: Binance book diffs arrive about every 100 ms, so with the 50 ms default the feature may never fire. | `lab micro-study ETHUSDT --leader BTCUSDT --lead-lag-interval 0.15` | `lead_lag_bps` has n > 0; interval chosen and written down. |
| C3 | Both directions: Bybit leads Binance and Binance leads Bybit, BTC leading ETH and SOL. | `lab micro-study SYMBOL --venue V --leader L --leader-venue W` | The decile move against a taker round trip, per pair, in §4. |
| C4 | If a pair passes a taker round trip at 150 ms: build `LeadLagTaker` on `FastStrategy`, backtest, holdout (as A7–A9). If it passes only as a maker signal: feed it into `PassiveMaker` as a quote skew. | `lab.fast`, `lab.maker` | Same gates as A7–A9. |

### Phase 1 exit gate

One strategy configuration with all of:

1. Research result with deflated Sharpe ≥ 0.95 over every configuration in the trial log for that data, qualified under A8b. An unavailable or provisional IID score cannot pass.
2. One-time holdout pass reserved under A9a: net P&L > 0 after fees and slippage, dependence-adjusted t ≥ 2.
3. Still positive at 150 ms latency (fast strategies) or at 10 bps slippage (carry).

---

## Phase 2: Paper trading ⏳

The Phase 1 survivor runs on live data with sandbox fills, during the hours the PC is on. The aim is a track record
that a funding decision can rest on.

| Step | Do | Module | Done when |
|---|---|---|---|
| 2.1 | **Persistent paper account.** Today the simulated account restarts at `--capital` every run, so P&L is per run. Store the session's closing equity, position and trade list, and resume from it. | `lab.fast`, `results/fast/` | Stop and restart: equity, open position and the round-trip list continue; test covers a kill mid-trade. |
| 2.2 | **Trade journal.** Every fill appended to `results/paper/trades.jsonl` with strategy, configuration fingerprint, latency from signal to fill, fee, and the book at the fill. | `lab.fast`, `lab.persistence` | Journal survives a killed process (append-only); round trips rebuilt from it match the node's report. |
| 2.3 | **Track-record report.** One command adds up every session: net P&L, trades, t of the mean trade, Sharpe on active days, max drawdown, P&L by hour of day and by session. | `lab paper report` (new) | Report on a synthetic journal matches a hand calculation in tests. |
| 2.4 | **Paper vs backtest.** Replay the recorder's data for the exact paper hours through `lab fast backtest` with the same configuration. Compare fills, fill prices and P&L. | `lab paper compare` (new) | Per session: fill count within ±20%, mean P&L per trade within the backtest's 95% interval. A larger gap is a model bug to fix before continuing. |
| 2.5 | **Monitor panel.** Show the cumulative track record (2.3) and the paper vs backtest gap (2.4) next to the per-run panel. | `lab.monitor`, `monitor_ui/` | Visible at `http://127.0.0.1:8766`; `/api/status` carries the same numbers. |
| 2.6 | **Carry paper runner** (only if Track B is the survivor). Wakes at 00, 08 and 16 UTC, computes the target positions from live funding, writes the simulated legs and funding payments to the journal. Missed funding times while the PC was off are logged as missed, not back-filled. | `lab carry paper` (new) | A week of runs; funding received matches the exchange's published rates. |
| 2.7 | **Operational drills**, each once, with the result written to the journal: exchange disconnect, Binance maintenance window, `lab kill`, `lab kill --flatten`, daily-loss stop, restart during an open position. | `lab.risk_manager`, `lab.safety` | Every drill ends flat or in the expected state, with the audit log showing why. |
| 2.8 | **Alerts.** Telegram on: daily-loss stop, kill switch, risk denial burst, feed silent > 1 min, paper vs backtest gap breach. | `lab.notify` | Each alert fires once in a drill. |

### Phase 2 exit gate (the funding gate)

All of, from the 2.3 report, on sessions after the configuration was frozen:

1. ≥ 300 round trips (fast) or ≥ 60 funding periods (carry), over ≥ 14 separate days.
2. Net P&L > 0 after fees, t-statistic of the mean trade ≥ 2.
3. Max drawdown ≤ 3% of paper capital, and no daily-loss stop caused by a bug.
4. Paper and backtest agree within the 2.4 bounds on ≥ 80% of sessions.
5. Every 2.7 drill passed.

Funding, and every spend from here on, is Gabriel's decision after this gate.

---

## Phase 3: Small live trading 💤

| Step | Do | Done when |
|---|---|---|
| 3.1 | Exchange account with trading-only API keys (no withdrawal permission), IP-whitelisted, stored outside the repo in `.env` / a secrets file. | Keys load on the host; `git` never sees them; CI checks for committed secrets. |
| 3.2 | Live execution client behind a config switch that defaults to sandbox, plus a startup check that refuses live mode without an explicit flag and a confirmation file. | Live mode cannot start by accident: tested. |
| 3.3 | Reconciliation: on start and every minute, compare the node's orders, fills and positions with the exchange's; any mismatch halts. | Injected mismatch halts in a test against the exchange testnet. |
| 3.4 | Testnet dress rehearsal: the full Phase 2 drill list on the exchange testnet with real order round trips. | All drills pass on testnet. |
| 3.5 | Live at the smallest size the venue allows, risk limits set to the funded capital: daily loss cap ≤ 1%, gross notional ≤ 1× capital. | 4 weeks live, P&L within the paper record's range, no limit breach, no reconciliation halt. |
| 3.6 | Scale size step by step (×2 at most every 2 weeks) while live P&L stays within the paper range. | Written decision per step. |

## Phase 4: Speed 💤

Only once live trading pays for it. Fast trading is the long-term focus; this is where it gets its hardware.

| Step | Do | Done when |
|---|---|---|
| 4.1 | Latency budget: measure tick-to-order and order-to-ack end to end, per component (ws receive, book update, strategy, risk checks, send). | Histogram per component in the monitor. |
| 4.2 | Always-on host near the matching engine (Binance: AWS Tokyo region) running recorder, monitor and live node. | 30 days uptime; recordings without holes. |
| 4.3 | Profile the Python hot path (feature updates, strategy callback, `RiskManager` checks). Port to Rust only the part the profile shows dominates, as a NautilusTrader-compatible component. | p99 tick-to-order under the budget set in 4.1; same results as the Python version on recorded data. |
| 4.4 | Revisit the parked sub-50 ms edges from Phase 1 with the new latency. | Each re-run as a counted trial with its own holdout. |

## Phase 5: More venues and asset classes 💤

The final system trades any market through the right broker. NautilusTrader adapters make venues pluggable; the
work per venue is data, fees, sessions and margin.

| Step | Do | Done when |
|---|---|---|
| 5.1 | Venue template: a checklist and test kit for adding a venue (recorder mapping, fee model, instrument precision, paper path), from the Bybit addition. | Written; used for the next venue. |
| 5.2 | More crypto venues (OKX, then a crypto options venue such as Deribit). | Each passes Phase 1 and 2 steps for its own strategies. |
| 5.3 | Trading calendars and session boundaries for markets that close: holidays, early closes, daylight-saving changes, overnight gaps in the risk manager. | Tests over calendar edge cases. |
| 5.4 | First non-crypto market (futures or forex) through a broker whose data and costs fit the budget at that time. | Same Phase 1 and 2 gates. |
| 5.5 | Portfolio-level risk across venues and currencies: limits in one base currency with conversion, correlated exposure. | Risk manager limits hold across two venues in a test. |

## Phase 6: AI strategist 💤

Longer-horizon trading where an AI proposes trades or strategy settings.

| Step | Do | Done when |
|---|---|---|
| 6.1 | A typed proposal format: instrument, direction, size, horizon, entry/exit rules, risk limits, rationale. Never raw orders. | Schema and validation tests. |
| 6.2 | Proposals go through the same pipeline as human research: counted in the trial log, backtested, holdout-tested. | A proposal can't reach paper without a holdout pass. |
| 6.3 | A person confirms any change to risk limits, markets or capital; the AI cannot raise its own limits. | Tested refusal of a self-raised limit. |
| 6.4 | Paper track record per AI strategy, same funding gate as Phase 2. | Gate passed. |

---

## Cross-cutting work (any time)

| Item | Why |
|---|---|
| Keep `README.md` and §4 of this file current after each study | The verdicts are the project's real state |
| Disk: compaction of old catalog files, or a cap per symbol | The monitor's disk runway shrinks with every recorded symbol |
| Data-quality checks in the monitor: crossed books, sequence gaps, stale mark price | A bad book silently ruins a maker study |
| Reproducibility: every study prints its catalog span, git commit and configuration fingerprint | A result has to be re-runnable |

## Not now

- Paid data, a VPS or co-location before the funding gate.
- Rewriting working Python in Rust without a profile showing it is the bottleneck.
- Any real-order code path without Gabriel's decision.
- New features for a strategy whose Phase 1 verdict is "no edge".

## Updating this file

Change a marker only with evidence on `main`: the PR, the command that was run and its output. Every study verdict
goes into §4 with its numbers, including the negative ones. Thresholds in the gates are changed only before the
data they judge has been looked at, with the reason in the commit message.

## Implementation evidence on this branch (not main acceptance)

| Step | Implementation | Evidence / still open |
|---|---|---|
| A4 | Implemented on `chatgpt/codex-maintenance` in `9c8e558`. | Five synthetic outage regressions failed before the fix; full suite: 143 passed on Python 3.13.16, lint and formatting passed. No real-data edge claimed. |
| A8 | Daily net equity and IID diagnostic implemented on this branch; statistical qualification remains A8b. | Full suite: 152 passed on Python 3.13.16, lint and formatting passed. Tests include UTC midnight, invalid/missing observations, trial-history compatibility, holdout exclusion, and the original paper's numerical example. A1-A3/A5 require real recordings, which are absent from this checkout. |
| A9a | Atomic holdout reservations implemented for fast and carry evaluations on this branch. | Full suite: 169 passed on Python 3.13.16, lint and formatting passed. Regression coverage includes competing processes, forced exit without cleanup, write failures, configuration/mode changes, frozen data cutoffs, legacy/corrupt evidence, and releases waiting for active evaluations. Holdout profitability and A6b/A7a/A8b qualification remain open. |

Methodology basis: Bailey and López de Prado, [The Deflated Sharpe Ratio (2014)](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf),
especially its discussion of selection bias and independent trials. The branch adds A6b/A7a/A8b/A9a before
examining candidate results, preserving the numerical profitability thresholds while clarifying what evidence
is sufficient to use them. A displayed statistic is an input to qualification, not automatic authorization.
