# trading-lab

A research-only trading system for US stocks, built as two cooperating layers of agents on
[NautilusTrader](https://nautilustrader.io) (Rust core, event-driven, identical code in
backtests and live trading):

- **Trading agents** (deterministic): turn signals into positions, enforce risk limits, place
  orders, keep the record.
- **Research swarm** (AI): proposes hypotheses, writes the code, reviews it. Deterministic code
  decides what survives.

It trades **paper accounts only**. Real money is refused at three independent layers.

```
 RESEARCH SWARM (runs on demand)                         TRADING AGENTS (message bus, every day)

 Researcher (AI) ── hypothesis ──▶ Engineer (AI)          SignalAgent × (signal, stock)
      ▲                              │ code                     │ target exposure 0..1
      │                         Sandbox checks ◀─ retry ─┐      ▼
      │                              │ pass              │  AllocatorAgent ──▶ RiskAgent ──▶ ExecutionAgent ──▶ broker
      │                         Critic (AI) ── issues ───┘                          ▲              │
      │                              │ accept                                       └── equity ────┤
      │                         Validator: train vs buy-and-hold + noise floor     RecorderAgent ◀─┘ fills, events
      │                              │ pass
      │                         Gate: test period, once ──▶ candidate
      └─ training results only ──────┘                        │ you: lab research promote
                                                              ▼
                                                     usable in paper trading
```

## Status: what has been verified, and where

| Component | Verified |
|---|---|
| Backtest engine, all agents, costs, fills, risk limits | Automated tests on real GOOG prices (44 tests) |
| Noise-floor test, holdout ledger, CLI | Automated tests + manual runs |
| Research swarm via Claude Code | Real end-to-end cycles (Researcher → Engineer → Sandbox → Critic → Validator → Gate) |
| Sandbox against malicious/broken generated code | Automated tests |
| Paper node construction + paper-only guards | Automated tests (node builds; no connection) |
| **Interactive Brokers connection, order flow** | **Not yet: needs your IB paper account.** First run: `lab paper --check-only`, then `lab paper` |
| **Yahoo Finance download** | **Not yet: the build machine had no internet.** First run: `lab data fetch` |
| **Ollama provider** | **Not yet: needs Ollama on your machine.** Check: `lab llm-check --provider ollama` |

## Install

Python 3.12–3.14. Uses [uv](https://docs.astral.sh/uv/) (`pacman -S uv` on Arch).

```bash
git clone <your repo> trading-lab && cd trading-lab
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest                      # 44 tests, ~5 s, offline
```

For the research swarm, either:
- **Claude Code** (uses your Claude subscription): install it, run `claude` once to log in, then `lab llm-check`.
  Usage counts against your plan's normal limits. If `ANTHROPIC_API_KEY` is set in your shell, Claude Code bills
  that key instead.
- **Ollama** (free, local): `ollama pull qwen2.5-coder:7b`, set `provider = "ollama"` and the model in `lab.toml`,
  then `lab llm-check`. An 8 GB GPU fits 7–8B models; expect weaker research than Claude.

## Quick start

```bash
lab data fetch                       # daily bars for the [universe] in lab.toml (Yahoo, adjusted, free)
lab backtest                         # built-in hypotheses vs buy-and-hold, training period
lab null-test sma_trend              # is the difference bigger than noise?
lab research run --cycles 3          # let the swarm propose, build and test 3 hypotheses
lab research list                    # outcomes;  lab research show <id>  for details
```

Offline check with real prices, no internet needed:

```bash
cd examples/goog-offline && lab data sample && lab backtest && lab null-test sma_trend
```

## The scientific protocol

The lab is built to make self-deception hard.

- **Three periods** (`[periods]` in lab.toml). *Train*: develop and optimize. *Test*: checked once, by the
  gate, only after a train pass. *Holdout*: the final exam, `lab holdout <signal>`, once per signal and
  parameter set. Every use is logged in `results/holdout_ledger.json`; repeats need `--again "reason"`,
  which is recorded too. `lab backtest` refuses the holdout period.
- **A control**: every result is shown against buy-and-hold on the same stocks, same costs.
- **A noise floor**: `null-test` reruns the comparison on block-bootstrapped copies of the real data
  (same return distribution, no long-range pattern). The p-value is how often noise did at least as well.
- **The swarm never sees test results**, so the test period stays out-of-sample across research cycles.
- **Multiple testing is counted**: the notebook states how many hypotheses were validated and how many
  would pass training by luck.
- **Honest fills**: decisions are made after the close and fill at the *next* session's real opening
  price, one tick of slippage, IBKR Pro Fixed commissions ($0.005/share, $1 minimum, 1% cap).
- **Provenance**: every saved run stores the config and the data manifest it used.

What a result needs before it means anything: beat buy-and-hold on *test*, beat the noise floor, hold
across several stocks, survive the holdout once, then weeks of paper trading.

## Paper trading (Interactive Brokers)

1. Open an IBKR account and enable its paper-trading account. Note the paper account ID (`DU…`).
2. Install IB Gateway (or TWS), log in **to the paper account**, and enable the API:
   *Configure → Settings → API → Settings*: enable socket clients, port **4002** (Gateway) or **7497** (TWS).
3. Set `account_id` in `[paper]` of lab.toml. Without IB market data subscriptions, keep
   `market_data = "DELAYED_FROZEN"`.
4. `lab paper --check-only --signals sma_trend` validates everything without connecting.
5. `lab paper --signals sma_trend --tickers SPY QQQ` starts the agents.

Daily cycle, New York time: at 16:10 the signal agents fetch the completed daily bars; at 16:15 the
execution agent sends **market-on-open** orders, which fill in the next morning's opening auction. The agents
keep their own cash ledger (`results/paper/ledger.json`, starting from `[paper] capital`), so other positions
in your paper account don't affect them. Every message is journaled to `results/paper/journal-YYYYMM.jsonl`.
Research hypotheses can be paper traded only after you approve them: `lab research promote <id>` shows the
code and asks.

**Paper-only, in three layers**: (1) before connecting, live ports (4001, 7496) and non-paper account IDs are
refused; (2) before *every* order, the execution agent checks that the broker reports exactly the configured
`DU…` account, and shuts the system down otherwise; (3) no code path builds a live-money configuration.

## Writing a signal

A signal is a pure function of one stock's past bars returning a target exposure between 0 and 1. The window
holds only bars that have already closed, so look-ahead is impossible by construction:

```python
from lab.signals.base import Signal, sma

class Breakout(Signal):
    name = "breakout"
    description = "Hold while the close is above its 100-day average."
    lookback = 100
    defaults = {"n": 100}

    def target(self, w):          # w.open/high/low/close/volume: numpy arrays, oldest first
        return 1.0 if w.close[-1] > sma(w.close, self.params["n"]) else 0.0
```

Add it to `BUILTIN` in `src/lab/signals/builtin.py`, then `lab backtest breakout`.
Parameters can be overridden per run: `lab backtest sma_trend --set sma_trend.fast=20`.

## Research swarm details

Each hypothesis gets a folder in `research/hypotheses/<id>/`: `proposal.json`, `signal.py`, `checks.json`,
`review.json`, `train.json`, `test.json`, `status.json` (with history) and `transcript.jsonl` (every prompt and
answer). A readable log accumulates in `research/NOTEBOOK.md`. Model calls are logged in
`research/usage.jsonl` and capped per cycle (`max_llm_calls_per_cycle`).

Generated code must pass a static check (one `Signal` subclass; imports limited to `math`, `numpy` and the
signal helpers; no file, network or process access; no `eval`/`exec`/`getattr`/dunder tricks), then a
behaviour check in a separate, resource-limited process. Validation also runs in a separate process. Static
checks stop accidents and obvious escapes, not a determined attacker: read code before you promote it.

Statuses: `proposed` → `implemented` → `validated` → `candidate` → `promoted` (by you), or
`broken`, `rejected_by_critic`, `failed_train`, `failed_test`, `error`.

## Layout

```
lab.toml                      all settings (universe, periods, costs, risk, paper, research)
src/lab/
  agents/                     the trading agents and their messages
  signals/                    Signal interface, built-in signals, registry
  research/                   providers (Claude Code, Ollama), roles, sandbox, orchestrator, validator worker
  backtest.py                 wires the agents into NautilusTrader's backtest engine
  experiments.py              comparison, noise floor, holdout ledger
  paper.py                    the same agents on Interactive Brokers' paper account
  data.py  universe.py  costs.py  metrics.py  safety.py  config.py  cli.py
tests/                        pytest suite (offline; `pytest -m llm` adds one real model call)
examples/goog-offline/        offline configuration using real GOOG prices
experiments/LOG.md            your lab notebook
```

## Known limitations

- **Survivorship bias**: the default universe is today's large, successful companies. Results on it look
  better than a strategy would have done choosing among the stocks that existed back then.
- **Yahoo Finance** data is free but unofficial. For serious work, import vendor data with
  `lab data import file.csv --ticker X --adjusted` (Databento offers $125 of free credits).
- Signals see one stock at a time: no cross-sectional strategies (e.g. "buy the top 3 momentum stocks") yet.
- Daily bars only. Long-only, no leverage. Idle cash earns no interest; brief overdrafts from opening gaps
  are allowed and reported (`min_cash`) but not charged interest.
- IBKR's $1 minimum is charged per fill; an order filled in pieces is slightly overcharged.
- NautilusTrader is pinned to `2.0.0rc7`: 2.0 is in release candidates and its API still changes between them.
  Upgrade deliberately and rerun the tests.

## License

LGPL-3.0-or-later, the same license family as NautilusTrader.
