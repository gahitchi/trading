# Read-only paper fee and funding settlement claims

**Status:** portable research validator and strict CLI, **not** an
authenticated account-settlement source and **not** economic posting.

Implementation: `lab.paper_settlement_claims`.
Tests: `tests/test_paper_settlement_claims.py`.

The existing `lab.sandbox_capture` stores NautilusTrader simulated
`OrderFilled` events, including the exact original commission Money
string, order IDs, trade IDs, fill times and account IDs.
`lab.paper_account_snapshot` binds local account positions to
verified historical order-receipt prefixes.
The new `ReadOnlySettlementReport` describes a separately supplied
**claim** about fee debits and funding payments for that specific
sandbox account. Its `collector_id`, source-kind label and completeness
assertions are **not proof** that an authorized independent provider
supplied it.

## Strict input contract

The report is a complete JSON object containing:
`report_id`, `collector_id`,
`source_kind="separate-readonly-settlement"`,
`session_id`, `account_id`, `instrument_id`,
`snapshot_id`, `capture_count`, `capture_sha256`,
`period_start_ns`, `observed_ns`, `complete_fees`,
`complete_funding`, `fee_items` and `funding_items`.

- Both completeness fields must be JSON booleans `true`.
  Their truth is self-asserted until independently proven.
- `fee_items`: complete list of objects with exactly
  `trade_id`, `event_ns`, `currency`, `amount`. The
  decimal-string amount must be nonnegative. Each entry must
  agree exactly with the corresponding captured fill's
  original fee Money and source event timestamp.
  Missing, duplicated, unknown or conflicting trade fees fail closed.
  A missing sandbox commission cannot be assumed to mean zero.
- `funding_items`: full asserted list of objects with exactly
  `payment_id`, `event_ns`, `currency`, `amount`.
  Amounts are signed exact decimals (payment/credit vs debit).
  A zero is not invented as a cash settlement; duplicate IDs,
  out-of-interval and future entries are prohibited. The
  specified start-to-observation period is not independently
  certified. An **empty** list does not prove that no payment
  occurred in the interval.
- Report and local snapshot must have the same session,
  account, instrument, snapshot ID, capture prefix SHA-256 and
  receipt count. Timestamp skew is bounded 0–5000 ms
  (default 1000 ms).
- Only exact finite decimal strings with bounded digits and
  fractional precision are accepted: binary floats, exponents,
  NaN, Infinity and absurdly long numbers are rejected. Values
  retain their specified currencies, **without** currency
  conversion, precision-loss fallback or rounding tolerance.
- The source snapshot's entire hash chain, checkpoint and
  the exact sandbox receipt prefix are replay-verified. Voided
  fills and duplicated source trade IDs block this comparison.

## Read-only CLI

With existing paper orders and snapshot files, plus a separately
supplied account report:

```bash
lab paper-settlement-check \
  --orders results/fast/paper-orders-SESSION.jsonl \
  --snapshots results/fast/paper-account-SESSION.jsonl \
  --report read-only-settlement-report.json \
  --session SESSION \
  --instrument BTCUSDT-PERP.BINANCE \
  --account SANDBOX_ACCOUNT_ID \
  --max-skew-ms 1000
```

The report must be a preexisting regular file at most 1 MiB.
The loader rejects symlinks, incomplete/malformed JSON, duplicate
keys (including nested objects) and unknown fields. The CLI
refuses absent source logs, heads and locks rather than creating
them as an accidental side effect. The command makes no network
requests and never touches the economic profit journal.

A report with consistent **fee claims** returns
`status=fee_claims_match_funding_unverified` and shows
unverified funding-payment counts and signed sums *by original
currency*, never netted across currencies.

Every successful diagnostic still returns:

```json
{
  "source_authenticated": false,
  "collector_independence_verified": false,
  "funding_payment_verified": false,
  "funding_coverage_verified": false,
  "settlement_balances_reconciled": false,
  "equity_reconciled": false,
  "margin_qualified": false,
  "risk_ready": false,
  "economic_posting_allowed": false
}
```

**Important distinction:** Binance public
`fundingRate` observations are historical *rates*, not proof of
specific wallet transfers. This validator requires explicit
account-specific payment IDs and amounts **supplied by a separate
collector**, which has **not** been implemented. The code checks
the structure of those claims but does not verify actual payment
or cash balance conservation against an independent custody
statement. It is *not* valid to fabricate funding items from
a rate and position notional, or to publish these claims as
`CashAdjustment` in `ProfitJournal`.

## Next qualification gates

1. Obtain a separately operated, authorized **read-only** sandbox
   account statement provider (without granting trading permissions)
   with authenticated account identity, complete fee/funding
   settlement IDs, stable timestamps, corrections and coverage.
2. Reconcile actual cash deltas, fee-currency conversion,
   open/close position lifecycle, partial fills, settlement
   corrections, contract multipliers and cross-restarts.
3. Bind real synchronized market marks and USD-M margin/liquidation
   specification; validate daily *net* equity and fees, including
   negative days and failed sessions, before considering economic
   posting or fractional-profit reinvestment.
4. Keep the new settlement comparator **off the order hot path** and
   live exchange permissions disabled.

Portable regression:
```bash
PYTHONPATH=src:. python -m pytest tests/test_paper_settlement_claims.py -q
```
