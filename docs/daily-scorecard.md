# UTC daily performance scorecard (research ledger only)

The source-backed implementation is `lab.daily_scorecard.daily_scorecard`,
with the command `lab daily-scorecard`. It reads **one verified, durable
`ProfitJournal` event sequence**, replaying its frozen `AllocationPolicy`
and demanding a complete UTC `day_close` on every calendar date through
the requested reporting end date. It is not connected to the Binance
demo importer, a live broker, the independent order system or a strategy.

## Scope, interpretation and limitations

The primary objective remains net **daily** marked-equity growth
across profitable, losing and flat days. There are no guarantees
of even minimal daily gains. The scorecard reports:

- Every closed UTC day, **including zero-trade days**, as a
  separate record with daily realized net P&L, change in
  open/unrealized P&L, signed external cash flows, total
  marked equity, daily return relative to **starting actual
  equity**, and cumulative net P&L.
- Trade **economic event** counts, active and inactive days,
  longest consecutive zero-trade streak, mean/median daily
  profit, empirical **nearest-rank 5th-percentile daily net
  P&L**, gross winning/losing-day amounts, profit factor and
  largest negative day. Economic trade entries are not
  independently verified exchange fills or necessarily
  completed round-trip positions.
- Exact ledger-reported trade commission, additional slippage
  and funding already included in signed trade P&L, plus
  separately booked signed funding adjustments. They are
  **diagnostic cost breakdowns** and never subtracted a
  second time from daily net P&L.
- Actual ending equity **including external cash flows** and
  a separate *flow-neutral* diagnostic series:
  `initial_capital + cumulative_net_PnL`. The latter excludes
  deposits and withdrawals, and drives drawdown and underwater
  duration metrics so an account deposit cannot erase a
  strategy loss. This is an absolute P&L benchmark, **not**
  a time-weighted return or risk-adjusted return series.
- Deployable and retained balances, reinvested profits and
  retained profits; these transfers do not create new
  marked equity or a better trading edge. The journal's
  reinvestment fraction is taken only from its **frozen
  policy** and never assumed by the scorecard.

A daily return percentage is reported only if that day's
`start_equity` is **positive**. Otherwise it is `null`
and the count of missing return denominators is disclosed.
Mean/median daily percentage-return diagnostics then apply
only to days with eligible denominators; they are **not**
comparable to an all-days percentage return in a dataset
containing such gaps. The daily *absolute P&L* statistics
always cover every eligible UTC day.

The flow-neutral drawdown starts at the initial capital
and uses end-of-UTC-day sampled equity. It is not an
intraday stress or liquidation-risk measure. The
`max_underwater_calendar_days` counts full UTC daily closes
below a prior high; `max_completed_recovery_calendar_days`
is `null` if no recovery has occurred, and the current
unrecovered age is reported separately. **Recovery is
not promised**.

The scorecard explicitly provides no fill-rate quality,
latency stress results, margin/liquidation qualification or
authentication of the underlying financial events.
A hashed local journal alone is not proof of real market P&L.

**Never** interpret:
`daily_edge_qualified=false`,
`source_authenticated=false`, `risk_ready=false`,
`economic_posting_allowed=false` as trade authorization.
The routine **does not write or amend** any economic event,
change risk caps, select a strategy or issue orders.

## CLI

The caller must provide the **exact original policy** and
preexisting durable economic journal. Example only (values
illustrate a test policy; these are **not** recommended
live risk or reinvestment defaults):

```bash
PYTHONPATH=src:. python -m lab.cli daily-scorecard \
  --journal results/research/profit-ledger.jsonl \
  --start-day 2026-11-05 \
  --through 2026-11-08 \
  --currency USD \
  --initial-capital 1000.00 \
  --quantum 0.01 \
  --reinvest-fraction 0.5 \
  --max-deployable 1500.00
```

The `--max-deployable` option is omitted only when the
source journal's frozen policy did not have a cap.
A policy mismatch is a **blocker**, not a signal to
overwrite the ledger. Likewise, the CLI requires the
existing journal, `.head.json` and `.lock`
rather than constructing an empty journal.

Offline acceptance:

```bash
PYTHONPATH=src:. python -m pytest tests/test_daily_scorecard.py -q
```

The tests cover variable small/large positive days, meaningful
losses, flat days, no-trade weeks, recovery and ongoing
underwater periods, external capital flows, fee/funding
double-booking protection, cross-midnight marks, frozen
0%/50%/100% reinvestment baselines, restart replays,
corrupted/missing checkpoints and the read-only CLI.

## Next independent qualification requirements

Authentic exchange/broker account evidence, ledger-compatible
contract specifications, completed fee/funding cash settlements,
realistic filled trade counts and timestamped market marks
are still missing. Statistical inference must correct for
multiple tested bearish strategies, correlated sessions,
holding times and fee/latency sensitivity before any
candidate can claim a profitable edge.
