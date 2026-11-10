# Time-ordered funding-settlement daily equity (2026-11-10)

**Research accounting only. No exchange orders, profitable-strategy claim,
liquidation simulation, or authorized paper/live execution.**

`src/lab/bearish_funded_equity.py` adds
`funded_daily_short_equity(frame, replay, costs, evidence, *, boundary,
expected_plan_sha256, assumptions=None, symbol="BTCUSDT")`.

This code is a **new, independent accounting model**. It does not change
the completed, negative BTC `btc-bearish-ensemble-ohlc-v1` historical
research registration, any trial results, or the protected
`2026-05-08T00:00Z` boundary. Do not repeat that experiment to
exercise these checks.

## Exact cashflow convention

1. The complete UTC-minute source is first validated using
   `daily_short_equity`: full closed UTC days, no missing minute,
   valid prices, pre-holdout only, and verified modeled trade prices
   and costs. This earlier **fixed-reserve calculation is a validator
   only**; its equity returns are never combined with the funded values.
2. The source-pinned settlement snapshot is independently rechecked
   against the exact **expected plan SHA-256** and the existing
   `short_funding_bounds` conservative ownership logic. If **any**
   settlement's ownership is ambiguous around an entry or exit,
   **the daily funding-backed equity is unavailable**. The separate
   funding-bounds audit still reports the pessimistic/optimistic
   range for those cases.
3. Cash-accounting actions are ordered by UTC nanosecond timestamps.
   At short entry, quantity is
   `settled_reference_cash × notional_fraction / entry_reference`,
   and the entry commission is charged immediately. Unrealized
   price gains are never available for reinvestment. At each certain
   exchange settlement timestamp, a short receives or pays
   `quantity × funding_rate × settlement_mark`, posted to cash at
   that instant. At model exit, price cashflow and exit commission
   are realized. **The replay's original fixed adverse funding
   reserve is NOT charged a second time.**
4. At each UTC date's **00:00 minute-end**, the ledger includes all
   previous settlements and completed exits and hypothetically
   buys to cover any still-open short at the observed closing
   reference plus adverse modeled spread/slippage and exit fees.
   No future settlement is credited or debited before its own
   timestamp. A new entry exactly at the boundary belongs to the
   following UTC day; an exit or funding posting exactly at the
   boundary is included in the preceding day.
5. Results include daily net P&L, daily return, settled
   reference-cash-equity (a modeling convention, **not a broker wallet
   balance**), open-position conservative marked value, and daily
   funding cashflow/count. No flat days are removed. Results also
   include total modeled equity return and close-to-close drawdown.
   An adverse candle-high equity-floor check with negative settlement
   funding fails closed; it is NOT a full exchange liquidation engine.

**Important restrictions:** Only trades with *unambiguously owned*
funding events have a funded equity report. This stringent requirement
is intentional and will exclude some otherwise valid historical
minute-bar trades. Such a failure is a **measurement limitation**, NOT
a rationale to drop those trades and declare a profitability sample.
The model cannot be used as an unqualified aggregate of the complete
strategy when any sample member fails. When a future full evaluation is
needed, integrate a rigorous liability/receivable interval ledger
instead of selecting only unambiguous trades.

## Test the implementation

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest tests/test_bearish_funded_equity.py -q
```

Eleven new synthetic cases exercise signed exchange funding at its
actual event time, positive funding receipts, trades without events,
midnight/overnight marking, reinvestment after settlement cashflows,
flat days, refused ambiguous ownership, source-digest errors,
holdout and gap protection, adverse funding loss stress and
source/trade immutability. Passes demonstrate code behavior only,
not a verified market edge.

## Outstanding critical prerequisites

- Reconcile independent order-book fills, fee tiers, actual account
  wallet funding and mark prices; do not use OHLC candles as executed
  prices or as authenticated mark-price evidence.
- Implement exchange-specific margin tiers, collateral haircuts,
  liquidation path and bankruptcy rules, including order rejects
  and partial fills.
- Extend strict source-time settlement accounting to conservatively
  bounded pending liabilities in the ambiguous case, without
  retrospective trade filtering or future knowledge.
- Future strategies require separate frozen research registrations,
  a prospective data split and independent calibration.
