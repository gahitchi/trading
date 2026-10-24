# Runbook: connecting trading-lab to an IBKR paper account

From zero to agents trading a paper account, with a verification step at every stage. Expect an afternoon,
plus IBKR's account-approval time.

## 1. IBKR account and paper account

1. Open an IBKR account (for EU residents this is Interactive Brokers Ireland). IBKR's documentation states that
   a paper trading account requires an **approved, funded** regular account; the EU account minimum is $0, but
   check the current conditions during sign-up.
2. Once approved, request a **paper trading account** in the IBKR Client Portal (account settings). You get a
   separate paper **username** and a paper **account ID starting with `DU`**. Write both down.
3. Market data: without subscriptions the paper account gets delayed data (15 minutes), which is fine at the
   daily speed (the agents act on completed daily bars). At a quick speed (1h, 15m, 5m) the delay is a real cost:
   keep `[trading] data_delay_min = 15` so the agents wait for each bar, or subscribe on the live account, enable
   market-data sharing with the paper account and set it to 0.

Check: log in to the paper account once with IBKR's normal Trader Workstation or the web portal and confirm the
`DU…` account ID and the simulated balance.

## 2. Host machine

**Quickest: the one-command installer** (README, Install): on Windows
`irm https://raw.githubusercontent.com/gahitchi/trading/main/install.ps1 | iex` in PowerShell, on Linux
`curl -fsSL https://raw.githubusercontent.com/gahitchi/trading/main/install.sh | sh`. It installs Docker and
Python when missing, asks for the paper login, starts the app and adds its icon. The manual steps follow.

Any Linux machine with Docker (your Arch PC, or an x86-64/ARM64 VPS):

```bash
sudo pacman -S docker docker-compose git uv      # Arch
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"                  # log out and in again
git clone https://github.com/gahitchi/trading.git && cd trading
```

At the daily speed the agents act once a day, but the machine has to be on at 16:10–16:15 New York time (normally
22:10–22:15 in Italy) and IB Gateway must stay logged in. At a quick speed they trade the whole session, 09:30–16:00
New York (15:30–22:00 in Italy).

### Two stages: your own computer for testing, then a cloud server

**Testing on your laptop or desktop.** Everything runs the same way in Docker. What differs is that the
machine must stay awake and online during the windows above:
- stop it suspending: `systemd-inhibit --what=sleep:idle:handle-lid-switch --why=trading docker compose up`
  for a session you watch, or set `HandleLidSwitch=ignore` and disable automatic suspend while testing;
- the stack restarts by itself after a reboot (`restart: always` / `unless-stopped`), and the agents resume
  from their ledger; a missed decision window is simply skipped, never traded late;
- IB Gateway restarts itself daily (05:00 New York) and IBKR asks for a full login about weekly; if it
  asks for a second factor, the gateway waits and retries (`TWOFA_TIMEOUT_ACTION: restart`).

**Moving to a cloud server** for real unattended operation, once the supervised session has passed:
- any x86-64 Linux VPS with 2 vCPU, 4 GB RAM and 20 GB disk (IB Gateway's Java uses about 1 GB), ideally on
  the US East coast near IBKR's servers; install Docker as above;
- SSH with keys only, and a firewall that allows SSH and nothing else. The gateway's API and VNC ports and the
  setup app are bound to 127.0.0.1 already; reach them through an SSH tunnel, e.g.
  `ssh -L 5900:127.0.0.1:5900 -L 8765:127.0.0.1:8765 you@server`, never by opening ports;
- `secrets/` and `.env` are created on the server itself (mode 600), never copied through Git;
- **one machine trades the account at a time.** The runtime lock only covers one machine: two copies on the
  same paper account would both trade. To move, stop the agents on your computer (ideally flat, or at least
  with no working orders), take a cold backup (`lab runtime backup --destination ...`, see
  docs/runtime-storage.md), copy it to the server with `scp`/`rsync` (it is private: it holds the control
  key), put its runtime directory at the same place (`results/paper/`) with the same `lab.toml`, check
  `lab recover` shows no halt, then start the server's stack. Start-up reconciliation compares the restored
  ledger with IBKR and hard-halts on any difference. Then remove the
  stack from your computer (`docker compose down`) so it cannot start again on reboot;
- set up Telegram alerts (§7) before leaving it alone: they are how you hear about halts, and with alerts
  configured, an alert outage longer than five minutes blocks new exposure (exits still go out);
- **before leaving it alone, check what is exposed**, on the server itself:
  1. `python3 tools/deployment_check.py` from the repository: every published port on 127.0.0.1, the password
     file readable only by you, `.env` and `secrets/` untracked by Git, a `DU` paper account, and no trading
     port listening on a public address. It exits 1 on any failure;
  2. from another machine, `nmap -Pn -p 1-65535 <server-ip>` should show SSH and nothing else;
  3. an independent AI penetration test, e.g. [Strix](https://github.com/usestrix/strix) (needs Docker and an
     LLM API key: `pipx install strix-agent`, then `strix --target ./` from the repository, and
     `strix --target https://...` only for something you deliberately expose). Read its findings in
     `agent_runs/`, fix what is real, and never point it at systems you do not own.

## 3. Configure

```bash
cp .env.example .env            # edit: TWS_USERID (paper username), LAB_PAPER_ACCOUNT (DU...), signals, tickers
mkdir -p secrets results data research
printf '%s' 'your-paper-password' > secrets/tws_password && chmod 600 secrets/tws_password
```

`lab.toml`, section `[paper]`: `capital` is how much of the paper account the agents may allocate (the
account may hold more: before each rebalance the agents check that the account can carry their book, and halt
if it cannot; set `dedicated_account = true` if the agents own the whole account, and then cash must match);
`max_order_value`, `max_orders_per_day` and `max_daily_turnover` are hard limits; `[risk]` holds the position cap,
gross cap and drawdown circuit breaker. `.env` and `secrets/` are git-ignored: never commit them.

**Risk profile and speed.** `[risk] profile` (calm, cautious, balanced, dynamic, aggressive; default balanced)
sets volatility target, drawdown halt, gross, shorts and net range together; `lab risk-profile` shows what is in
force. `dynamic` and `aggressive` short and lever up, and only they may set `[trading] speed` to `1h`, `15m` or
`5m` (README, "Risk profiles and trading speeds"). Start a quick speed with `lab paper --check-only` (it refuses
setups beyond IBKR's data pacing) and a day of watching the journal.

**Shorts and leverage (optional).** The balanced default is long-only and unlevered. Choose the dynamic or
aggressive profile, or use `profile = "custom"` and raise `[risk] max_short_gross`, `max_gross` and the net range,
and `[portfolio] max_leverage` or `leverage` (README, "Long/short, leverage and margin"). First check in IBKR's Client Portal that the account is a **margin**
account with short-selling permissions (paper accounts normally mirror the live account's type). Set
`[margin] debit_apr` to IBKR's current rate for your balance. Shorts IBKR cannot borrow are rejected: the agents
report it and stay flat in that stock. Watch `borrow_fees` and `financing` in `ledger.json` against IBKR's
statement: the ledger's figures are estimates, IBKR's are what you pay.

## 4. Start IB Gateway

```bash
docker compose up -d ib-gateway
docker compose logs -f ib-gateway     # wait for the login to complete
```

To watch the gateway's screen (useful the first time, or for login dialogs), set `VNC_SERVER_PASSWORD` in `.env`,
restart the gateway, and connect a VNC viewer to `127.0.0.1:5900`.

If login fails with the paper username, IBKR may require the main username with paper mode selected. The gateway
image is [gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker); its README documents the login
options (`TWS_USERID_PAPER`, 2FA handling).

## 5. Pre-flight

From the host (the compose file exposes the paper API on `127.0.0.1:4002`):

```bash
uv sync --frozen && source .venv/bin/activate
LAB_PAPER_ACCOUNT=DU1234567 lab doctor
```

Everything except "agents heartbeat" (they aren't running yet) should be ✓ or an understood warning.

## 6. Smoke test: no trading

```bash
docker compose run --rm -e LAB_IB_CLIENT_ID=8 lab paper --smoke
```

This connects, verifies that IB reports exactly your `DU…` account, loads every instrument, requests daily bars
for each, lists any positions, then disconnects. Then the full order-path check:

```bash
docker compose run --rm -e LAB_IB_CLIENT_ID=8 lab paper --smoke --test-order
```

It places a **real** 1-share paper order, a limit buy at half the last close, waits for IB to accept it, and
cancels it. It normally does not fill. If it does (a gap down through the limit, or a fill before the cancel
lands), the smoke test sends a closing sell at once and FAILS, naming both orders: look at the paper account.
Expected:

```
  ✓ paper account: broker reports ['IB-DU1234567'], expected DU1234567
  ✓ instruments: 2 loaded
  ✓ historical bars: SPY: 9 bars, last close ...
  ✓ broker positions: {}
  ✓ test order: submitted BUY 1 SPY LIMIT ...; accepted by the broker; cancelled
smoke test PASSED
```

| Symptom | Likely cause |
|---|---|
| cannot connect / timeout | gateway not logged in yet (check logs or VNC), or wrong port |
| "client id already in use" | another process uses that client id: smoke tests use `LAB_IB_CLIENT_ID=8`, the agents use 7 |
| instrument missing | ticker/venue in `[universe]` doesn't match IB's primary exchange (e.g. ETFs on `ARCX`) |
| no historical bars | market-data permissions; keep `market_data = "DELAYED_FROZEN"` |
| paper account check fails | `LAB_PAPER_ACCOUNT` differs from what IB reports, or a live account is logged in: STOP |

## 7. Alerts (optional, recommended)

Create a Telegram bot with @BotFather, send it a message, read your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`, put both in `.env`, then:

```bash
docker compose run --rm lab notify-test
```

You'll get a message at startup, a daily equity report, fills, and every risk event.

## 8. Start the agents

```bash
docker compose up -d
docker compose ps                  # lab becomes "healthy" once its heartbeat is fresh
docker compose logs -f lab
```

First real decision: the next weekday at 16:15 New York time; orders fill at the following open.

## 9. Daily operations

The agents run unattended within an active authority mandate. The first start creates a 24-hour mandate; restart does not renew it. See [operating controls](docs/operator-controls.md) for explicit renewal and supervised/manual operation. What they do when something goes wrong:

- **Transient trouble resumes by itself.** A broker or gateway disconnect, the gateway's daily restart (05:00 AM
  New York; pending-order reconciliation is still required), a container restart, a late or missing daily bar, a rejected order. Docker
  restarts the agents; at start-up they re-check the paper account, reconcile positions, cash and open orders with
  IBKR, and book any fills that happened while they were down (from IBKR's reports, once per trade ID). Then they
  carry on. You get a Telegram message (`auto_resumed`, `fill_recovered`, `stale_data`, ...) but nothing to do.
- **Hard halts wait for you.** The drawdown circuit breaker (positions are exited first), a broker account other
  than your `DU…` paper account, or a ledger/broker difference that fill recovery cannot explain. The halt is the
  file `results/paper/HALT`: it survives restarts, and no order goes out (except the breaker's exits) until you
  clear it with `lab recover`.

| Task | Command |
|---|---|
| status | `docker compose ps`, `cat results/paper/heartbeat.json` (`hard_halt`, `pending_orders`, `kill_switch`) |
| performance, costs, implementation shortfall | `lab paper-report` |
| stop all new orders now | `docker compose exec lab lab kill --reason "..."` (add `--flatten` to also exit positions) |
| undo the kill switch | `docker compose exec lab lab resume` |
| see a hard halt | `docker compose exec lab lab recover` |
| clear a breaker or account halt | `docker compose exec lab lab recover --reason "what you checked"` |
| ledger/broker mismatch | check the positions in IBKR; if IBKR is right: `lab recover --reason "..." --adopt-broker` (adoption requires terminal broker reports for every pending order; unknown intents remain journaled and halted) |
| unconfirmed order (`order_unknown`) | in TWS or the Client Portal, open **Trades** and the order history for the days since the journaled time and look for the client order ID in the halt. If IBKR shows no such order and no execution: `lab recover --reason "..." --dispose-order ID --evidence "what you saw, where, which dates"`. The next start disposes of it only if IBKR still does not report it and the stock's position matches the ledger; the intent and your evidence stay in the ledger, and a fill that still arrives is booked and halts again. If IBKR shows the order, let the agents settle it from its reports instead |
| change strategy | stop and obtain a reviewed identity/state migration plan before changing `LAB_SIGNALS` / `LAB_TICKERS`; changing the environment alone is refused |
| trade a research hypothesis | `lab research promote <id>` (reads you the code first), then add it to `LAB_SIGNALS` |
| update | use `lab runtime backup` and `lab runtime preflight`; follow [the reviewed code-upgrade workflow](docs/runtime-storage.md) before starting the new image |

Every cleared halt is recorded with your reason in `results/paper/recoveries.jsonl`.

Runtime files in `results/paper/`: `ledger.sqlite3` (authoritative transactional cash,
positions, orders and permanent trade history), `ledger.json` (small diagnostic projection),
`risk.json` (equity peak for the drawdown limit), `journal-YYYYMM.jsonl` (every message), `heartbeat.json`,
`KILL` (kill switch), `HALT` (hard halt), `recoveries.jsonl`, `smoke-*.json`.

**Tested before it touches IBKR.** `tests/test_harness.py` runs this exact node (same agents, same start-up
reconciliation) against a simulated IBKR paper broker: a week of trading must reproduce the backtest fill for fill;
crashes between order and fill, restarts with open orders, rejects, partial fills, a wrong account, the kill
switch, every hard halt and its recovery are each a test.

## 10. Judging the paper test

Run it for weeks, not days. Then compare against the backtest of the same signals over the same dates:

- `lab paper-report`: return, drawdown, commissions, and **implementation shortfall** (fill price vs the close the
  decision was based on). The backtests assume the next open plus one tick; if the measured shortfall is much
  worse, the backtests are too optimistic.
- The decisions themselves should match the backtest day by day: the journal has every signal and target.

A strategy is worth more attention only if it passed the research gates, its holdout run, and paper trading
without surprises.

## Candidate release acceptance and state upgrades

Development remains open. Use [the candidate acceptance protocol](docs/paper-acceptance.md) before a supervised
IBKR paper session; it defines evidence, failure scenarios and pass rules for all seven release gates.

The authoritative execution ledger is now SQLite; its small diagnostic JSON projection
uses schema **v3** so older JSON-only binaries refuse it. Legacy v1/v2 JSON is validated
and migrated on first opening. Terminal/disposed attribution and trade IDs remain indexed
on disk and never expire automatically; the 2 GiB capacity limit stops new execution on
exhaustion. The notification outbox migrates transactionally with monotonic IDs and bounded
admission. Never remove history records to free space or downgrade a runtime in place.

For an upgrade, stop all writers; back up the entire paper runtime (including SQLite journal/WAL files if
present), configuration, exact old source/image identity and the new candidate. Verify broker orders, fills,
positions and cash against the backup. Runtime identity deliberately refuses an implementation/configuration
change: this release does **not** provide an automatic identity-reset/migration command. Have the reviewer
approve the migration with preserved evidence before changing identity. A rollback must restore a mutually
consistent runtime and implementation only after reconciling all broker activity since the backup; restoring
an old ledger over later fills is unsafe. Do not run old and new binaries concurrently on the same account.

If ledger persistence fails, the node latches a submission refusal and requests shutdown. Do not simply restart
until storage is healthy and the broker reports have been reconciled against the last durable ledger. An atomic
replacement may already have succeeded when directory fsync failed; inspect the actual file, not assumptions
about the error. Local injected-fault tests are not power-loss or actual-broker qualification.


## Release candidate: operating controls and storage

See [operator-controls.md](docs/operator-controls.md) for autonomous, manual and
supervised mandates, and [runtime-storage.md](docs/runtime-storage.md) for the
SQLite migration, private cold backups and broker-attested code upgrade. The default
autonomous mandate is standing (no expiry); an opt-in lease (`--authority-hours N`) or a
supervised/manual mandate is never renewed by a restart. Never edit the diagnostic ledger
JSON or restore it over the permanent trade history. The default notification
outage rule blocks new exposure after five minutes of undelivered alerts.
