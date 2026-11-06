# Causal public mark ↔ paper account snapshot alignment

**Status:** read-only research diagnosis; no equity, funding settlement, or risk authorization.

The portable `lab.paper_mark_alignment.align_paper_account_mark` operation
joins two *preexisting*, replay-verified archives:

1. A NautilusTrader local sandbox account journal tied to an exact
   sandbox-order receipt prefix (`lab.paper_account_snapshot`).
2. A separate, public Binance USD-M mark and historical funding-rate
   journal (`lab.public_market_evidence`).

The result is a time-bound **observation association**, not a valuation
or independent proof of actual venue executions. The public market
source does not know the account's orders or wallet.

## Conservative selection rules

The operation loads and verifies **every row and durable checkpoint**
of both sources before pairing. It uses the **latest** validated account
snapshot and the newest public-mark pair that was **fully observed**
before that snapshot. Specifically:

- Session IDs and the exact `BTCUSDT-PERP.BINANCE` instrument ID
  must agree. No other markets, collateral currencies or contract
  multipliers are inferred.
- The *entire paired REST acquisition*, including historical funding
  response, must complete **no later than** account snapshot time.
  Reading a newer sample and assigning its price retroactively to an
  older account snapshot is prohibited.
- The venue's mark timestamp must not be later than the account snapshot,
  even if the REST parser tolerates small venue clock skew.
- Venue mark age and collection completion age must both be within
  `--max-mark-age-ms` (default **5000 ms**, adjustable 0–60000 ms).
  There is no stale-price interpolation or forward filling.
- Account/source histories remain unchanged, including after malformed
  inputs. Missing journals, checkpoint/lock files and inconsistent
  clocks or account scopes cause a blocked result.

The REST source uses Unix-epoch nanoseconds for local request times and
Unix-epoch milliseconds for venue timestamps. The sandbox snapshot's
`captured_ns` must be in the **same epoch nanosecond clock domain**.
This assumption is not independently verified: deterministic Nautilus
replays with historical/epoch-zero timestamps **must not** be aligned
with present-day public REST responses. An identical timestamp alone
does not establish synchronized clocks or independent custody.

## CLI (read only)

With archives previously created by the opt-in paper and public REST
collectors, run:

```bash
lab paper-mark-align \
  --orders results/fast/paper-orders-SESSION.jsonl \
  --snapshots results/fast/paper-account-SESSION.jsonl \
  --market results/fast/public-mark-SESSION-BTCUSDT.jsonl \
  --session SESSION \
  --symbol BTCUSDT \
  --account SANDBOX_ACCOUNT_ID \
  --max-mark-age-ms 5000
```

The command checks for the existence of all three source journals,
heads and lock files **before** initializing any archive reader. It
prints a structured diagnostic including the selected account
snapshot ID, event-prefix hash and count, mark observation completion
time, exact mark/index decimal strings, associated historical
funding-rate metadata, and observed timestamp age.

Every successful pairing explicitly returns:

```json
{
  "status": "temporally_aligned_unqualified",
  "funding_payment_verified": false,
  "account_source_authenticated": false,
  "clock_domain_independently_verified": false,
  "equity_reconciled": false,
  "margin_qualified": false,
  "order_state_complete": false,
  "risk_ready": false,
  "economic_posting_allowed": false
}
```

It does **not** compute USDT P&L, unrealized returns, available
margin, liquidation levels, fees or funding payments. The observed
historical funding **rate** is not a cash settlement. No trading
permissions, profit journal updates or automatic reinvestment are
connected to this result.

## Validation and next dependencies

Portable tests: `PYTHONPATH=src:. python -m pytest tests/test_paper_mark_alignment.py -q`.
They cover matching and mismatched scope, no-lookahead, future venue
timestamps, stale prices, bounded age override, account/market source
corruption, complete archive replay and read-only CLI behavior.

The remaining qualification work is to capture synchronized book/mark
observations in an actual public-data paper session, independently
obtain complete order/wallet/account funding settlement reports,
establish contract specifications and margin/liquidation multipliers,
and reconcile realized and unrealized daily net equity before enabling
**any** economic posting or risk sizing. Live orders remain disabled.
