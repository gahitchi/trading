# trading-lab

> **Experimental multi-market branch** — This branch adds an entirely separate, additive crypto-first architecture.
> The original IBKR US-stock application remains paper-only and broker-dependent until an individually
> tested migration. The multi-market CLI is an offline reference simulator, **not yet** a connected demo
> broker, certified matching engine, deployable autonomous crypto bot or live-money trading system.
> See [the architecture](docs/architecture/multimarket-2026-10-25.md) and the
> [institutional application-flow and open-source review](docs/audits/2026-10-25-institutional-flow-open-source-review.md)
> for verified capabilities, unimplemented release gates and source/license selection.

## Multi-market proof of concept (this branch only)

After the standard `uv sync --frozen --extra dev` installation, no broker credentials are needed:

```sh
uv run python -m lab.markets demo
uv run python -m lab.markets demo --side buy --quantity 0.001 --balance 1000
uv run python -m lab.markets demo --journal ./results/offline-reference.sqlite3
uv run python -m lab.markets recover --journal ./results/offline-reference.sqlite3
uv run python -m lab.markets native-demo
uv run python -m lab.markets native-fill-parity
```

Both commands run a **synthetic** trade fixture and a next-observed-trade simulator, demonstrating long/short
contracts, the 24/7 UTC minute aggregator, adverse slippage, taker commissions and operator risk ceilings.
They do not make real orders or claim a profitable strategy. The CLI accepts `--csv path.csv` with
`timestamp,price,quantity,trade_id,mark_price` fields; the timestamps must have UTC/explicit timezone offsets. The independent mark-price column is **required**. In the bundled synthetic fixture it is hypothetical, not an exchange-validated mark feed.
External data from an arbitrary CSV is not automatically qualified as trustworthy market data.

For real, **read-only**, public OKX Europe completed one-minute candles (requires network, no credentials):

```sh
uv run python -m lab.markets candles --instrument BTC-USDT --limit 20
uv run python -m lab.markets candles --instrument BTC-USDT --limit 100 --store ./results/candle-catalog
uv run python -m lab.markets observations --instrument BTC-USDT-SWAP --limit 20
uv run python -m lab.markets microstructure --instrument BTC-USDT-SWAP --depth 5
uv run python -m lab.markets history --instrument BTC-USDT --pages 5 --limit 100 --store ./results/candle-catalog
```

A stored OFFLINE_SIM journal can be deterministically verified and resumed only with **matching account,
instrument, fee, and risk assumptions**; truncated or inconsistent evidence is rejected. This is local
reference-simulator recovery, not broker/exchange reconciliation. Existing journals are never silently reset.
The optional candle catalog stores immutable, SHA-256-addressed UTC-day Parquet partitions and preserves
missing-interval metadata, but it does **not** qualify their source completeness or order-execution quality.
The observations command fetches the latest EEA-hosted public swap mark and recent executed trades as
**separate timestamped feeds**, preserving aggressor side and source labels. The new microstructure command
fetches an independently timestamped REST order-book depth snapshot and **indicative** funding rate, not
a gap-free streaming L2 feed, confirmed account funding cashflows, verified maker fills, or authority to trade.
The history command retrieves up to 50 bounded pages of confirmed 1-minute bars through the EEA history
endpoint, enforcing chronological cursors without silently forward-filling gaps; it cannot claim complete
historical coverage or point-in-time quote/fee/funding provenance.
The broker-neutral pure risk preflight (`lab.markets.portfolio_risk`) screens pending
orders against stressed positions, shared collateral, initial margin, drawdown,
daily/absolute loss, funding buffers and three fill fractions per order (up to
six simultaneous orders). It is a **bounded screening oracle**, not proof
that every arbitrary partial fill or exchange margin tier is safe. The
offline adapter exposes its current pending book to this preflight without
bypassing mandatory fill-time risk. A separate pure **demo-only**
reconciliation checker (`lab.markets.reconciliation`) compares unknown,
partially filled and cancelled orders, positions, and exact collateral by
currency. It does not authenticate venue data, settle money, authorize
orders, or qualify a live/demo exchange connection.
The `native-demo` command also runs the bundled **synthetic, credential-free** trade and independent
mark fixture through the real Nautilus BacktestEngine, with a margin account, explicitly configured
nonzero maker/taker fees at the native venue (the model's required location for trading fees) and a
read-only Nautilus DataActor that verifies every event was delivered at its receipt timestamp.
This pilot has **no strategy, order routing, filled orders, fees actually charged or broker connection**.
The separate `native-fill-parity` diagnostic does generate **two actual Nautilus simulated
market-order fills** against four **explicitly synthetic** top-of-book quotes. It compares
native taker fee charges, price/quantity, a flat terminal position, and the native margin
account with an independent exact-Decimal linear-contract oracle. This verifies only
that fixed controlled round-trip and its cost accounting. It is **not** market-data
provenance, accurate order-book queue execution, maker/partial-fill parity, verified
funding, strategy profitability, broker-demo settlement, crash recovery or authority
to trade money. A passing result is a narrow component qualification, not a release gate.

Neither the native runtime nor its fixture proves a profitable strategy or execution-cost parity.

The offline-only Nautilus bridge now constructs native `CryptoPerpetual` instruments from explicit,
scenario-supplied contract metadata (including multiplier, minimum notional and margin
assumptions; maker/taker BPS are retained separately because pinned Nautilus 2.0.0rc7
cannot accept them in its native perpetual constructor),
and converts observed trades, independently priced marks, and confirmed candles into native `TradeTick`,
`MarkPriceUpdate`, and `Bar` objects. It preserves each source receipt timestamp and refuses
duplicate or time-regressing observations in its read-only causal tape. Mark prices retain their
own precision instead of being rounded to the tradable order tick. These are *model/data boundaries*:
their assumptions are not qualified venue schedules or connected accounts.
**This is a running read-only Nautilus crypto data-engine pilot, not yet a crypto Nautilus
trading strategy, order-management service or qualified broker.**
The browser app has a **Try crypto without a broker** synthetic-sample panel and a separate
**Inspect public crypto market data** read-only panel with token-protected access, before IBKR setup.
Neither panel starts autonomous trading. The paper-trading plan and its broker-dependent Start button
remain distinct.

For operator-side verification without secrets, see
[credential-free acceptance checks](docs/acceptance/multimarket-credential-free.md).

This does not trade or backtest those candles; it deliberately excludes unfinished bars, and historical
bar OHLCV is not sufficient to simulate realistic exchange fills. See the D2/D3/D6 gates in the design.
The existing `lab paper` IBKR deployment below is unchanged. Live-money orders are **not** implemented.


[![ci](https://github.com/gahitchi/trading/actions/workflows/ci.yml/badge.svg)](https://github.com/gahitchi/trading/actions/workflows/ci.yml)

A paper-only US-stock research system **in development**, built on
[NautilusTrader](https://nautilustrader.io) (Rust core, event-driven, identical code in backtests and live trading).

Release readiness is blocked until the criteria in [docs/release-criteria.json](docs/release-criteria.json) are met.
Run `python3 tools/release_check.py` for the current blockers. Passing automated tests alone is not release qualification;
see the [remediation review](docs/audits/2026-10-23-remediation-and-release-review.md).

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
| Risk profiles; quick trades at 1h / 15m / 5m (delayed data, flat at close, intraday holdout) | automated tests (`tests/test_intraday*.py`), a compressed intraday session through the real paper node |
| Kill switch, reconciliation, daily caps, declared membership exits | automated tests through full backtests |
| PSR / Deflated Sharpe / PBO / bootstrap CI | formula and bounded synthetic tests; general selection-aware calibration remains open |
| Research swarm via Claude Code | real end-to-end cycles (propose → implement → review → validate → gate) |
| Docker image and compose stack | GitHub Actions builds and runs the image on every push |
| **The paper node itself** (same agents, start-up reconciliation, restarts, rejects, partial fills, hard halts) | **runs against a simulated IBKR paper broker (`lab.sim`, `tests/test_harness.py`); a week of trading, long-only and long/short, matches the backtest fill for fill** |
| **IB Gateway connection, order routing** | **your first run: [RUNBOOK.md](RUNBOOK.md), `lab paper --smoke --test-order`** |
| **Yahoo Finance download, Ollama provider** | **your first run: `lab data fetch`, `lab llm-check --provider ollama`** |

## Install

**The app (paper trading on your computer), one command.** It installs what is missing, downloads Trading
Lab, asks for your IBKR paper login once, starts it and adds a Trading Lab icon. Run the same command again
to update; your settings, credentials and history are kept. Nothing trades until you press Start.

Windows 10/11, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/gahitchi/trading/main/install.ps1 | iex
```

Arch Linux with KDE: `bash install-arch.sh` from a clone, see [docs/arch-install.md](docs/arch-install.md).

On Windows it installs Docker Desktop and Python with `winget` if they are missing. Docker Desktop's first
start asks you to accept its terms and may install WSL 2 and ask for a restart; after the restart, run the
command again. The first start builds the program (5 to 15 minutes); later starts take seconds. Before
trading you need an approved IBKR account with a paper account (`DU...`), see [RUNBOOK.md](RUNBOOK.md).

**The research lab (command line).** Python 3.13 or 3.14 (NautilusTrader 2.0 requires it), Linux or macOS.
Uses [uv](https://docs.astral.sh/uv/) (`pacman -S uv` on Arch).

```bash
git clone https://github.com/gahitchi/trading.git && cd trading
uv sync --frozen --extra dev
source .venv/bin/activate
pytest -m "not llm and not harness" -n auto   # fast offline suite, in parallel (a few minutes)
pytest -m harness      # real-clock recovery scenarios against the simulated broker (several minutes)
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
| `lab data fetch / import / membership / sample / list [--speed]` | market data (daily or intraday) and the point-in-time universe |
| `lab backtest [signals] [--period] [--set p=v] [--target-vol] [--speed]` | strategies vs buy-and-hold, HTML report |
| `lab risk-profile` | the limits the selected profile really trades with |
| `lab null-test`, `lab pbo`, `lab walk-forward` | noise floor, overfitting probability, walk-forward analysis |
| `lab holdout <signal> [--speed]` | the one-time final exam (ledgered; intraday has its own) |
| `lab research run / list / show / retry / promote` | the AI research swarm |
| `lab llm-check`, `lab notify-test`, `lab doctor` | provider check, alert check, pre-flight check |
| `lab paper [--mode autonomous/supervised/manual] [--bank] [--smoke] [--check-only]` | paper trading on Interactive Brokers |
| `lab control status / approve / renew` | inspect expiring authority, approve an exact proposal, or set manual targets |
| `lab runtime backup / preflight / upgrade` | cold backups and broker-evidenced code upgrades |
| `lab kill [--flatten]`, `lab resume`, `lab health [--ready]` | kill switch, liveness (Docker) / readiness |
| `lab recover [--reason] [--adopt-broker]` | show or clear a hard halt (audited) |

## The scientific protocol

The lab is designed to make self-deception hard.

- **Three periods**: *train* to develop, *test* checked once by the gate, *holdout* for a final exam
  (`lab holdout`, once per signal and parameter set, every use ledgered; `lab backtest` refuses it).
- **A control**: every result is shown against buy-and-hold on the same stocks with the same costs.
- **Conditional bootstrap comparison**: repeats the comparison on block-resampled histories and reports an empirical tail probability under that model; general false-positive and power qualification remains open.
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
  stock's data ends early, a run requires a declared membership exit with subsequent real prices; otherwise it is refused. EOF does not establish delisting proceeds or a foreknown exit.

Survivorship-free data with a membership file is what makes results credible; free data gives a biased first look.

Intraday bars (`--speed 1h|15m|5m`) live in `data/intraday/<speed>/`, regular hours only, indexed by bar start in
New York time. Yahoo serves about 2 years of 1h bars and about 60 days of 15m and 5m bars; each `lab data fetch
--speed ...` keeps what earlier fetches stored (rebased onto the newest split/dividend adjustment), so fetching
every few weeks builds a longer history. Every intraday result states how many sessions it rests on.

## Writing signals

A per-stock signal is a pure function of one stock's past bars (prices, volume, session dates, time of day)
returning a target exposure in [-1, 1] (negative = short); the window holds only closed bars, so look-ahead is
impossible by construction. At a quick speed each element is one intraday bar and `lookback` counts bars;
`w.minute` is the minutes since the 09:30 open at each bar's close (390 for daily bars).

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

Built-in intraday techniques (quick speeds only): `orb`, the opening range breakout (Crabel; Zarattini & Aziz
2023: long above the first 30 minutes' high, short below its low, stop at the midpoint, one trade per session,
optional "stocks in play" relative-volume filter); `intraday_momentum`, market intraday momentum (Gao, Han, Li &
Zhou 2018: the first half hour's return predicts the last half hour); `vwap_reversion` (fade a stretch from the
session VWAP beyond a band of its own deviations, out at VWAP); `value_area`, a volume-profile failed auction
(Steidlmayer's Market Profile; Dalton's "80% rule": when price leaves the previous session's value area, the 70%
of its volume around the point of control, and closes back inside it, go for the opposite edge; the profile is
approximated from bars, so finer bars approximate it better). They are hypotheses like the rest: they earn
trust only through the protocol, on enough sessions.

## Research swarm

Each hypothesis lives in `research/hypotheses/<id>/`: `proposal.json`, `signal.py`, `checks.json`, `review.json`,
`train.json`, `test.json`, `status.json` and `transcript.jsonl` (every prompt and answer). `research/NOTEBOOK.md`
is the readable log. Model calls are capped per cycle and logged in `research/usage.jsonl`.

Generated code must pass a static check (one `Signal` subclass; imports limited to `math`, `numpy` and the signal
helpers; no file, network, process or introspection access), and it is **never imported by the lab**: every
research signal, in checks, backtests and paper trading alike, runs in its own worker process
(`lab.research.isolation`). Before compiling source, the worker installs a default-deny Linux seccomp
filter that blocks filesystem, network, process creation and privilege-changing syscalls. It also closes
inherited descriptors, caps memory/CPU/files/descriptors, and drops root when the host permits it. Linux
with `libseccomp.so.2` is required; installation failure refuses the signal. IPC reads and writes have
deadlines. It shares the kernel (it is not a VM), so still read the code before you promote it.

`lab research promote` records `approval.json`: the hash of the exact source you were shown, of its train and
test evidence, and the approved parameters. Both validation runs must already bind that source, parameters
and configuration; changing code before promotion cannot reuse old evidence. Paper trading refuses changes
to risk, portfolio, costs, margin, trading speed, capital or the validated universe. Existing unbound
candidates must be revalidated, then promoted again. The research swarm currently requires daily speed.

Statuses: `proposed` → `implemented` → `validated` → `candidate` → `promoted` (by you), or `broken`,
`rejected_by_critic`, `failed_train`, `failed_test`, `error`. The AI swarm writes per-stock signals;
cross-sectional strategies are human-written for now.

## Paper trading and deployment

Step by step: **[RUNBOOK.md](RUNBOOK.md)**. This is a release candidate pending the
[external acceptance gates](docs/release-criteria.json), not an approved production release.

The first `lab paper` start creates a **standing autonomous mandate**: it does not expire, so
unattended, continuous paper trading is the default. A time limit is opt-in (`--authority-hours N`,
at most 168); supervised and manual modes (`--mode`) are always leases, 24 hours by default, and a
restart never renews an expired lease. `lab control renew --mode supervised` revokes a standing
mandate; `lab kill` stops trading. See [operating controls](docs/operator-controls.md) and
[storage and recovery](docs/runtime-storage.md).

Deployment commands:

```bash
cp .env.example .env                    # TWS_USERID, LAB_PAPER_ACCOUNT (DU...), optional Telegram
printf '%s' 'password' > secrets/tws_password && chmod 600 secrets/tws_password
docker compose up -d ib-gateway
docker compose run --rm -e LAB_IB_CLIENT_ID=8 lab paper --smoke --test-order
docker compose up -d                    # agents run daily; health, alerts, journal, ledger on disk
```

Daily cycle (New York time): 16:10 the signal agents fetch completed daily bars from IB; 16:15 the execution agent
reconciles its ledger with IB and sends **market-on-open** orders; 09:30 they fill in the opening auction.

### Sharing capital among stocks

`[portfolio] weighting` decides how a per-stock strategy's capital is split among its stocks: `equal` (default),
`inverse_vol` (each position carries a similar risk rather than a similar amount of money) or `hrp`
(Hierarchical Risk Parity, López de Prado 2016: correlated stocks share one risk budget). Both are estimated only
from prices the agents have already seen, and fall back to equal weights until 20 shared returns exist. They are
implemented in numpy (`lab.allocation`) rather than with skfolio, whose scikit-learn/cvxpy dependencies would
weigh on the locked Docker build for two methods. Compare them like any hypothesis: `lab backtest` with each
setting on the same period, and keep one only if it beats `equal` out of sample.

### Long/short, leverage and margin

Signals and cross-sectional strategies return signed targets (negative = short); `lab.toml` decides what is
traded. The defaults are long-only and unlevered. To enable shorts and leverage, select `[risk] profile =
"dynamic"` or `"aggressive"` (always bounded by `[limits]`), or set `[risk] profile = "custom"` and raise
`[risk] max_short_gross`, `max_gross`, the net range `min_net`/`max_net`, and `[portfolio] max_leverage`
as appropriate. Under a **named preset**, the loader derives effective risk settings from the profile and
`[limits]`; editing the custom numeric `[risk]` settings does not increase the preset's allowances. Use
`lab risk-profile` to inspect the actual ceilings. The broker's rules bound all of it: `[margin]` holds
Reg T / FINRA requirements by default, so gross exposure can never be configured above 2x. The risk agent keeps the targets' initial margin
inside equity with a cushion, deleverages by itself when equity nears the maintenance requirement, and turns a
breach into a `margin_call` hard halt during which positions may only shrink. Borrow fees and interest on
borrowed cash accrue for every night a book is held; their rates are assumptions (no free point-in-time borrow
data), and every result says so. Built-ins: `sma_trend --set short=1` (trend long/short) and `momentum_ls`
(dollar-neutral cross-sectional momentum).

### Risk profiles and trading speeds

`[risk] profile` sets how hard the bots trade; `[limits]` is the operator's ceiling over every profile (by default
the broker's maximum, so the profile decides). `lab risk-profile` prints what is really in force.

| profile | target vol | drawdown halt | gross | shorts | net range | quick trades |
|---|---|---|---|---|---|---|
| calm | 6% | 8% | 0.3x | none | 0 .. 0.3 | no |
| cautious | 10% | 12% | 0.5x | none | 0 .. 0.5 | no |
| **balanced** (default) | 14% | 18% | 1.0x | none | 0 .. 1.0 | no |
| dynamic | 20% | 25% | 1.35x | up to 0.30x | -0.30 .. 1.10 | 1h, 15m, 5m |
| aggressive | 28% | 30% | 1.8x | up to 0.75x | -0.75 .. 1.60 | 1h, 15m, 5m |

`[trading] speed` is `daily` (decide after the close, fill at the next open) or, for dynamic and aggressive only,
`1h`, `15m` or `5m`: the agents then decide on every bar of the regular session. With IBKR paper data, bars are
15 minutes late (`data_delay_min = 15`; set 0 with a real-time subscription). In backtests a bar reaches the agents
only when the delayed feed would have it (rounded up to whole bars) and orders fill at the next opening print; live,
new exposure goes out as a limit order 1% (`limit_collar`) through the last price, cancelled and re-priced if still
working a bar later, and exits as market orders. `flat_at_close = true` (default) exits everything in the session's
last interval, using a timer independent of market-data arrival. Rejected orders, outages and exchange
closures can still prevent an exit; being flat overnight is an objective, not a guarantee. Order and turnover caps count every
rebalance of the day. Intraday research initially reserves the last 20% of stored sessions
(`intraday_holdout`) and persists that boundary in `results/intraday_partition.json`; later downloads do
not move reserved sessions into training. `[trading] intraday_holdout_start` sets the boundary deliberately
(e.g. when the partition was first made from 1h history and leaves 15m none), never after a holdout already run.
This is a separate protocol because free intraday history falls inside the daily holdout, not proof of
independence from prior daily research. The research swarm works on daily bars, and its promoted signals trade
at the daily speed only. New exposure waits while earlier orders are unsettled; where a reduction has not filled,
`lab.reservations` checks possible independent-fill outcomes against **all** profile exposure
(per-stock, gross, short-book, and net) and initial-margin limits, not merely gross exposure.
Reservations also account for fees, slippage, and separately specified adverse-price stress;
they are not a guarantee against future market gaps. IBKR paces historical-data requests
(60 per 10 minutes), so the node refuses setups that would exceed about 50: at 5m that is
25 stock-strategy pairs.

## Safety

- **Paper-only, three layers**: (1) before connecting, live ports (4001, 7496, Docker 4003) and non-`DU` account
  IDs are refused; (2) at start-up and before every order, the execution agent checks that IB reports exactly the
  configured paper account, and otherwise halts (a hard halt) and shuts the system down; (3) no code path builds a
  live configuration.
- **Kill switch**: `lab kill` stops new orders and cancels open ones within a minute; `--flatten` also exits all
  positions at the next open; `lab resume` clears it. An unreadable kill file still halts.
- **Unattended within an active authority mandate**: transient trouble (disconnects, restarts, late data, rejects)
  resumes by itself once the start-up checks pass again; hard halts (drawdown breaker, wrong account, an unexplained
  ledger/broker difference) survive restarts until `lab recover --reason ...` clears them (RUNBOOK §9).
  Authority expiry requires explicit renewal and cannot be reset by a supervisor restart.
- **Reconciliation**: every order is journaled before it is sent; fills that happen while the agents are down are
  booked from IB's reports at start-up, once per trade ID. Before every rebalance the ledger must match IB's
  positions, or it is a hard halt. `lab recover --adopt-broker` can take IB's positions only after all
  pending orders have terminal reports. It cannot erase unknown order intents; a broker snapshot alone
  does not prove that a late fill is impossible. After independently checking IBKR's **order and trade
  history** for an unreported order, the operator can resolve that specific intent using
  `lab recover --dispose-order ID --evidence "what IBKR history confirms" --reason "why disposal is safe"`.
  Never dispose an order without evidence that it cannot fill later.
- **Tested against a simulated IB paper broker**: the paper node itself, not a copy, runs in `tests/test_harness.py`
  (lab.sim), including a week of trading that must match the backtest fill for fill.
- **Limits**: per-order notional cap, orders per day, daily buy turnover, NautilusTrader's order-rate limit, risk
  agent position/gross caps and drawdown circuit breaker.
- **Alerts** (Telegram) for selected risk events, fills and a daily equity report; heartbeat-based Docker health check.

## Layout

```
lab.toml                    every setting: universe, periods, costs, risk, portfolio, paper, research
Dockerfile, docker-compose.yml, .env.example, RUNBOOK.md
src/lab/
  agents/                   existing Nautilus signal, allocation, risk, execution and monitoring agents
  signals/                  Signal / CrossSectionalSignal interfaces, built-ins, registry
  research/                 AI providers, roles, sandbox, orchestrator and validator worker
  markets/                  experimental D1 crypto domain, shared linear accounting, offline SIM,
                            one-minute data, read-only OKX EEA candles, journal and adapter contracts
  backtest.py               existing equity agents wired into NautilusTrader's backtest engine
  experiments.py            research comparisons, trial registry, walk-forward and holdout ledger
  stats.py                  PSR, DSR, PBO (CSCV), bootstrap CI
  paper.py                  existing Interactive Brokers paper-only runtime
  allocation.py             stock portfolio-weighting methods
  reservations.py           worst-case independent-order-fill exposure and margin checks
  margin.py  risk_profiles.py
                            equity financing and profile/operator limits
  execution_store.py        durable execution and recovery bookkeeping
  authority.py              operator mandates and execution authority
  strategy_bank.py          experimental approved-strategy portfolio bank
  capture.py                read-only evidence capture
  runtime_ops.py            runtime recovery and upgrade operations
  provenance.py             simulation and whole-runtime implementation digests
  data.py  universe.py  costs.py  metrics.py  report.py  notify.py  safety.py  config.py  cli.py
tests/                      pytest suite (offline; `pytest -m llm` adds a real model call)
examples/goog-offline/      offline configuration on real GOOG prices
experiments/LOG.md          your lab notebook
```

## Known limitations

- The optional daily strategy bank is experimental and has no whole-policy promotion claim.
  The bounded synthetic power pilot does not qualify research power or false-positive control.
- **Original `lab` pipeline: US stocks only.** `lab backtest` and `lab paper` still use equity-specific
  instruments, sessions, Reg-T margin, and the IBKR paper account lifecycle. Multi-market support has
  been authorized and is under active development in `lab.markets`: currently D1 offline crypto
  linear-derivative reference simulation and **read-only** OKX Europe one-minute candles.
  Neither live crypto/forex trading nor an externally connected crypto demo executor is implemented.
  See the [multi-market ADR](docs/architecture/multimarket-2026-10-25.md).
- **Free data is survivorship-biased** and unofficial; see [Data](#data).
- Idle cash and short-sale proceeds earn no interest unless `[margin] credit_apr` / `short_rebate_apr` are set
  (ASSUMED rates).
- Backtests execute at split- and dividend-adjusted prices; per-share commissions use the recorded split history,
  but fully unadjusted execution prices are not modelled yet.
- Intraday: free history is short (weeks for 15m/5m), so intraday statistics are much weaker than daily ones.
  Backtests round data delay up to whole bars and replay sparse opening prints; this is an approximation,
  not a guaranteed conservative execution model. The pinned exchange calendar handles regular holidays and
  early closes; unscheduled halts and actual broker-adapter timestamp parity still need acceptance evidence.
- Without configured historical borrow evidence, borrow fees use one assumed rate for every stock: there is no free historical borrow data, so hard-to-borrow
  names are underpriced in backtests. In paper trading IBKR charges the real fee; the ledger's estimate is
  reported next to it. The simulated broker refuses shorts without borrowable shares but does not liquidate
  positions itself: the agents' own deleveraging and margin-call halt are what is tested.
- IBKR's $1 minimum is charged per fill, so an order filled in pieces is slightly overcharged.
- NautilusTrader is pinned to `2.0.0rc7` (2.0 is in release candidates); upgrade deliberately and rerun the tests.
- The existing IBKR integration remains **paper-only**; the RUNBOOK smoke test is still required to
  verify the broker lifecycle end to end. No live-money order route is authorized or enabled.

## Collaboration

Two AI agents build this together by reviewing each other's work: Claude owns `main`, ChatGPT owns its own
branches, and each writes only to its own. They talk through immutable messages in their own outboxes
(`python3 tools/collab.py show <thread>` merges both sides), and Gabriel decides disagreements. Rules:
[COLLABORATION.md](COLLABORATION.md).

## License

LGPL-3.0-or-later, the same license family as NautilusTrader. For research and education; not investment advice.
