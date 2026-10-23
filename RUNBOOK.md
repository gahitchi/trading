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

Any Linux machine with Docker (your Arch PC, or an x86-64/ARM64 VPS):

```bash
sudo pacman -S docker docker-compose git uv      # Arch
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"                  # log out and in again
git clone https://github.com/gahitchi/trading.git && cd trading
```

At the daily speed the agents act once a day, but the machine has to be on at 16:10–16:15 New York time (normally
22:10–22:15 in Italy) and IB Gateway must stay logged in. At a quick speed they trade the whole session, 09:30–16:00
New York (15:30–22:00 in Italy). A small always-on VPS avoids depending on your PC.

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
uv venv && source .venv/bin/activate && uv pip install -e .
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

The agents run unattended and continuously by default. What they do when something goes wrong:

- **Transient trouble resumes by itself.** A broker or gateway disconnect, the gateway's daily restart (11:45 AM
  New York, when nothing is pending), a container restart, a late or missing daily bar, a rejected order. Docker
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
| change strategy | edit `LAB_SIGNALS` / `LAB_TICKERS` in `.env`, `docker compose up -d lab` |
| trade a research hypothesis | `lab research promote <id>` (reads you the code first), then add it to `LAB_SIGNALS` |
| update | `git pull && docker compose build lab && docker compose up -d` |

Every cleared halt is recorded with your reason in `results/paper/recoveries.jsonl`.

Runtime files in `results/paper/`: `ledger.json` (the agents' cash, positions and orders sent but not yet settled),
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
