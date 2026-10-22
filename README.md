# trading-lab

[![ci](https://github.com/gahitchi/trading/actions/workflows/ci.yml/badge.svg)](https://github.com/gahitchi/trading/actions/workflows/ci.yml)

A research-grade, paper-only trading system for US stocks, built on
[NautilusTrader](https://nautilustrader.io) (Rust core, event-driven, identical code in backtests and live trading).

Two layers of cooperating agents:

- **Trading agents** (deterministic, on NautilusTrader's message bus): signals → allocation → risk → execution,
  with a recorder and alerting. The same agents run in backtests and on an Interactive Brokers paper account.
- **Research swarm** (AI): a Researcher proposes falsifiable hypotheses, an Engineer implements them, a Critic
  reviews them. Deterministic code (sandbox, validator, statistical gates) decides what survives.

**Paper accounts only.** Real money is refused at three independent layers (see [Safety](#safety)).

```
 RESEARCH SWARM (on demand)                               TRADING AGENTS (every trading day)

 Researcher (AI) ── hypothesis ──▶ Engineer (AI)          SignalAgent × stock  ┐
      ▲                              │ code               CrossSectionalAgent  ┘ targets
      │                     Sandbox: static + behaviour         ▼
      │                              │ ◀── retry ──┐      AllocatorAgent (equal-weight, point-in-time, vol target)
      │                         Critic (AI) ── issues           ▼
      │                              │ accept             RiskAgent (position, gross, drawdown breaker)
      │       Validator: train vs buy-and-hold,                 ▼
      │       noise floor, Deflated Sharpe                ExecutionAgent ─▶ broker (only component that orders)
      │                              │ pass                     │ ledger · reconciliation · kill switch · caps
      │       Gate: test period, once ──▶ candidate       RecorderAgent · AlertAgent (Telegram)
      └── training results only ─────┘                │ you: lab research promote
```

## Contents

- [What is verified, and where](#what-is-verified-and-where)
- [Install](#install) · [Quick start](#quick-start) · [Commands](#commands)
- [The scientific protocol](#the-scientific-protocol)
- [Data](#data) · [Signals](#writing-signals) · [Research swarm](#research-swarm)
- [Paper trading and deployment](#paper-trading-and-deployment) · [Safety](#safety)
- [Layout](#layout) · [Known limitations](#known-limitations)

## What is verified, and where

| Component | Verification |
|---|---|
| Backtest engine, agents, costs, next-open fills, ledger = simulator account | automated tests, real GOOG prices |
| Long/short, leverage, margin calls, borrow fees and debit interest | automated tests (`tests/test_long_short.py`) |
| Kill switch, reconciliation, daily caps, delistings, point-in-time universe | automated tests through full backtests |
| PSR / Deflated Sharpe / PBO / bootstrap CI | tests incl. calibration (false-discovery control, 95% coverage) |
| Research swarm via Claude Code | real end-to-end cycles (propose → implement → review → validate → gate) |
| Docker image and compose stack | GitHub Actions builds and runs the image on every push |
| **The paper node itself** (same agents, start-up reconciliation, restarts, rejects, partial fills, hard halts) | **runs against a simulated IBKR paper broker (`lab.sim`, `tests/test_harness.py`); a week of trading, long-only and long/short, matches the backtest fill for fill** |
| **IB Gateway connection, order routing** | **your first run: [RUNBOOK.md](RUNBOOK.md), `lab paper --smoke --test-order`** |
| **Yahoo Finance download, Ollama provider** | **your first run: `lab data fetch`, `lab llm-check --provider ollama`** |

## Install

Python 3.13 or 3.14 (NautilusTrader 2.0 requires it), Linux or macOS. Uses [uv](https://docs.astral.sh/uv/) (`pacman -S uv` on Arch).

```bash
git clone https://github.com/gahitchi/trading.git && cd trading
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest                 # ~70 tests, ~15 s, offline
lab doctor             # what is ready, what is missing
```

Research swarm, pick one:
- **Claude Code** (your Claude subscription): install it, run `claude` once to log in, then `lab llm-check`.
  Usage counts against your plan's limits; if `ANTHROPIC_API_KEY` is set, that key is billed instead.
- **Ollama** (free, local): `ollama pull qwen2.5-coder:7b`, set `provider = "ollama"` in `lab.toml`.

## Quick start

```bash
lab data fetch                          # daily bars for [universe] in lab.toml (Yahoo, adjusted, free)
lab backtest                            # built-in hypotheses vs buy-and-hold; HTML report saved per run
lab null-test sma_trend                 # is the edge bigger than noise?
lab pbo sma_trend --grid fast=20,50,100 slow=150,200      # probability the "best" parameters are overfit
lab walk-forward sma_trend --grid fast=20,50 slow=150,200 # rolling optimization, out-of-sample record
lab research run --cycles 3             # the AI swarm proposes, builds and tests 3 hypotheses
```

Offline check on real prices: `cd examples/goog-offline && lab data sample && lab backtest`.

## Commands

| | |
|---|---|
| `lab data fetch / import / membership / sample / list` | market data and the point-in-time universe |
| `lab backtest [signals] [--period] [--set p=v] [--target-vol]` | strategies vs buy-and-hold, HTML report |
| `lab null-test`, `lab pbo`, `lab walk-forward` | noise floor, overfitting probability, walk-forward analysis |
| `lab holdout <signal>` | the one-time final exam (ledgered) |
| `lab research run / list / show / retry / promote` | the AI research swarm |
| `lab llm-check`, `lab notify-test`, `lab doctor` | provider check, alert check, pre-flight check |
| `lab paper [--smoke] [--test-order] [--check-only] [--adopt-broker]` | paper trading on Interactive Brokers |
| `lab kill [--flatten]`, `lab resume`, `lab health [--ready]` | kill switch, liveness (Docker) / readiness |
| `lab recover [--reason] [--adopt-broker]` | show or clear a hard halt (audited) |

## The scientific protocol

The lab is designed to make self-deception hard.

- **Three periods**: *train* to develop, *test* checked once by the gate, *holdout* for a final exam
  (`lab holdout`, once per signal and parameter set, every use ledgered; `lab backtest` refuses it).
- **A control**: every result is shown against buy-and-hold on the same stocks with the same costs.
- **Noise floor**: the comparison repeated on block-bootstrapped copies of the real data; p = how often noise did as well.
- **Deflated Sharpe Ratio** (Bailey & López de Prado, "The Deflated Sharpe Ratio", 2014): every
  configuration ever evaluated on a dataset is logged in `results/trials.jsonl`; the DSR of the active return
  charges for all of them. The research gate requires DSR ≥ 0.95.
- **Probability of Backtest Overfitting** via combinatorially symmetric cross-validation, and **walk-forward
  analysis** for anything with parameters.
- **Bootstrap confidence intervals** on the active Sharpe ratio.
- **The swarm never sees test results**, and a hypothesis that met the data can't be retried (`retry` refuses).
- **Honest fills**: decisions after the close, fills at the *next* session's real opening price, one tick of
  slippage, IBKR Pro Fixed commissions. Ledger and simulator account must agree to the cent (tested).
- **Provenance**: each saved run stores the config, data manifest and data-quality flags with a self-contained
  HTML report (equity, drawdown, rolling active Sharpe, monthly returns, all statistics).

Research runs measure the *strategy*: no per-stock caps, no circuit breaker (which would corrupt the control).
Paper trading always enforces both.

## Data

Every dataset carries quality labels (source, adjusted, survivorship-free) that follow it into every result;
reports and tables print a warning when the data is biased.

- `lab data fetch`: Yahoo Finance, free, split/dividend adjusted, **not survivorship-free**.
- `lab data import file.csv --ticker X --adjusted --survivorship-free --source VENDOR`: any vendor's CSV
  (date, open, high, low, close, volume), e.g. exports from IBKR or vendors that include delisted stocks.
- `lab data membership members.csv`: a point-in-time universe (`ticker,start,end`; several spells per ticker
  allowed). Stocks are only held while they are members; capital is shared among the members of each day; when a
  stock's data ends (delisting) the agents exit at its last real open.

Survivorship-free data with a membership file is what makes results credible; free data gives a biased first look.

## Writing signals

A per-stock signal is a pure function of one stock's past bars (prices, volume, session dates) returning a target
exposure in [0, 1]; the window holds only closed bars, so look-ahead is impossible by construction.

```python
from lab.signals.base import Signal, sma

class Breakout(Signal):
    name = "breakout"
    description = "Hold while the close is above its 100-day average."
    lookback = 100
    defaults = {"n": 100}

    def target(self, w):              # w.open/high/low/close/volume/dates: numpy arrays, oldest first
        return 1.0 if w.close[-1] > sma(w.close, self.params["n"]) else 0.0
```

A cross-sectional signal (`CrossSectionalSignal`) ranks stocks against each other and returns weights
(see `Momentum`, Jegadeesh & Titman 12-1 momentum). Register signals in `BUILTIN` in
`src/lab/signals/builtin.py`.

## Research swarm

Each hypothesis lives in `research/hypotheses/<id>/`: `proposal.json`, `signal.py`, `checks.json`, `review.json`,
`train.json`, `test.json`, `status.json` and `transcript.jsonl` (every prompt and answer). `research/NOTEBOOK.md`
is the readable log. Model calls are capped per cycle and logged in `research/usage.jsonl`.

Generated code must pass a static check (one `Signal` subclass; imports limited to `math`, `numpy` and the signal
helpers; no file, network, process or introspection access), and it is **never imported by the lab**: every
research signal, in checks, backtests and paper trading alike, runs in its own worker process
(`lab.research.isolation`). The worker closes inherited file descriptors, limits itself to three (no files,
no sockets), may write no file and start no process, has memory and CPU caps and drops root, all before it
compiles the code; the lab exchanges only price windows and fixed-format numbers with it. It shares the
kernel (it is not a container), so still read the code before you promote it.

`lab research promote` records `approval.json`: the hash of the exact source you were shown, of its train and
test evidence, and the approved parameters. Paper trading refuses the signal if any of them has changed.

Statuses: `proposed` → `implemented` → `validated` → `candidate` → `promoted` (by you), or `broken`,
`rejected_by_critic`, `failed_train`, `failed_test`, `error`. The AI swarm writes per-stock signals;
cross-sectional strategies are human-written for now.

## Paper trading and deployment

Step by step: **[RUNBOOK.md](RUNBOOK.md)**. In short:

```bash
cp .env.example .env                    # TWS_USERID, LAB_PAPER_ACCOUNT (DU...), optional Telegram
printf '%s' 'password' > secrets/tws_password && chmod 600 secrets/tws_password
docker compose up -d ib-gateway
docker compose run --rm -e LAB_IB_CLIENT_ID=8 lab paper --smoke --test-order
docker compose up -d                    # agents run daily; health, alerts, journal, ledger on disk
```

Daily cycle (New York time): 16:10 the signal agents fetch completed daily bars from IB; 16:15 the execution agent
reconciles its ledger with IB and sends **market-on-open** orders; 09:30 they fill in the opening auction.

### Long/short, leverage and margin

Signals and cross-sectional strategies return signed targets (negative = short); `lab.toml` decides what is
traded. The defaults are long-only and unlevered. To allow shorts and leverage, raise `[risk] max_short_gross`,
`max_gross`, the net range `min_net`/`max_net`, and `[portfolio] max_leverage` (the ceiling for volatility
targeting) or `leverage`. The broker's rules bound all of it: `[margin]` holds Reg T / FINRA requirements by
default, so gross exposure can never be configured above 2x. The risk agent keeps the targets' initial margin
inside equity with a cushion, deleverages by itself when equity nears the maintenance requirement, and turns a
breach into a `margin_call` hard halt during which positions may only shrink. Borrow fees and interest on
borrowed cash accrue daily in the ledger; their rates are assumptions (no free point-in-time borrow data), and
every result says so. Built-ins: `sma_trend --set short=1` (trend long/short) and `momentum_ls` (dollar-neutral
cross-sectional momentum).

## Safety

- **Paper-only, three layers**: (1) before connecting, live ports (4001, 7496, Docker 4003) and non-`DU` account
  IDs are refused; (2) at start-up and before every order, the execution agent checks that IB reports exactly the
  configured paper account, and otherwise halts (a hard halt) and shuts the system down; (3) no code path builds a
  live configuration.
- **Kill switch**: `lab kill` stops new orders and cancels open ones within a minute; `--flatten` also exits all
  positions at the next open; `lab resume` clears it. An unreadable kill file still halts.
- **Unattended by default, with two kinds of stop**: transient trouble (disconnects, restarts, late data, rejects)
  resumes by itself once the start-up checks pass again; hard halts (drawdown breaker, wrong account, an unexplained
  ledger/broker difference) survive restarts until `lab recover --reason ...` clears them (RUNBOOK §9).
- **Reconciliation**: every order is journaled before it is sent; fills that happen while the agents are down are
  booked from IB's reports at start-up, once per trade ID. Before every rebalance the ledger must match IB's
  positions, or it is a hard halt (`lab recover --adopt-broker` takes IB's positions deliberately).
- **Tested against a simulated IB paper broker**: the paper node itself, not a copy, runs in `tests/test_harness.py`
  (lab.sim), including a week of trading that must match the backtest fill for fill.
- **Limits**: per-order notional cap, orders per day, daily buy turnover, NautilusTrader's order-rate limit, risk
  agent position/gross caps and drawdown circuit breaker.
- **Alerts** (Telegram) for every risk event, fills and a daily equity report; heartbeat-based Docker health check.

## Layout

```
lab.toml                    every setting: universe, periods, costs, risk, portfolio, paper, research
Dockerfile, docker-compose.yml, .env.example, RUNBOOK.md
src/lab/
  agents/                   signal, cross-sectional, allocator, risk, execution, recorder, alerts, smoke test
  signals/                  Signal / CrossSectionalSignal interfaces, built-ins, registry
  research/                 providers (Claude Code, Ollama), roles, sandbox, orchestrator, validator worker
  backtest.py               wires the agents into NautilusTrader's backtest engine
  experiments.py            comparison, trials registry, noise floor, PBO, walk-forward, holdout ledger
  stats.py                  PSR, DSR, PBO (CSCV), bootstrap CI
  paper.py                  the same agents on Interactive Brokers' paper account
  data.py  universe.py  costs.py  metrics.py  report.py  notify.py  safety.py  config.py  cli.py
tests/                      pytest suite (offline; `pytest -m llm` adds a real model call)
examples/goog-offline/      offline configuration on real GOOG prices
experiments/LOG.md          your lab notebook
```

## Known limitations

- **Free data is survivorship-biased** and unofficial; see [Data](#data).
- Daily bars only; idle cash and short-sale proceeds earn no interest (conservative).
- Borrow fees use one assumed rate for every stock: there is no free historical borrow data, so hard-to-borrow
  names are underpriced in backtests. In paper trading IBKR charges the real fee; the ledger's estimate is
  reported next to it. The simulated broker refuses shorts without borrowable shares but does not liquidate
  positions itself: the agents' own deleveraging and margin-call halt are what is tested.
- IBKR's $1 minimum is charged per fill, so an order filled in pieces is slightly overcharged.
- NautilusTrader is pinned to `2.0.0rc7` (2.0 is in release candidates); upgrade deliberately and rerun the tests.
- The IB live path is built against the adapter's API and verified up to connecting; the RUNBOOK's smoke test
  is the first end-to-end check.

## Collaboration

Two AI agents build this together by reviewing each other's work: Claude owns `main`, ChatGPT owns its own
branches, and each writes only to its own. They talk through immutable messages in their own outboxes
(`python3 tools/collab.py show <thread>` merges both sides), and Gabriel decides disagreements. Rules:
[COLLABORATION.md](COLLABORATION.md).

## License

LGPL-3.0-or-later, the same license family as NautilusTrader. For research and education; not investment advice.
