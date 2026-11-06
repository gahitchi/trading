# Two-source paper account comparison (read-only and unqualified)

Implementation: `src/lab/paper_account_crosscheck.py`
Tests: `tests/test_paper_account_crosscheck.py`
CLI: `lab paper-crosscheck`

This is the next research **comparison contract**, not a connection to an
independent broker data service. The NautilusTrader local sandbox account
is already captured separately by `lab.nautilus_snapshot`. The second
source must still be supplied by an operator or a future separately
operated, authorized, read-only provider. Neither `collector_id` nor
`source_kind` verifies that it came from a different system. No trading
credentials, internet source or order endpoint is used here.

## Scope and source requirements

The `ReadOnlyAccountReport` JSON schema contains exactly:

| Field | Meaning |
| --- | --- |
| `report_id`, `collector_id` | Stable report and collector labels; **not authenticated** |
| `source_kind` | Literal `separate-readonly-report`; an assertion, not proof |
| `session_id`, `account_id`, `instrument_id` | Must match the existing sandbox evidence |
| `observed_ns` | Source observation time in the **same Unix-epoch nanosecond clock domain** as the sandbox snapshot |
| `snapshot_id` | Must identify the **latest** durable sandbox snapshot |
| `capture_count`, `capture_sha256` | Must agree exactly with the corresponding validated source receipt prefix |
| `complete_positions`, `complete_orders`, `complete_balances` | Exact JSON booleans `true`; merely operator/source assertions |
| `positions` | Full signed quantities by instrument, as exact decimal strings |
| `open_orders` | Full client-order ID to remaining quantity mapping, as exact decimal strings |
| `balances` | Full currency mapping, each with decimal-string `total`, `available`, `locked` |

The provider must be separately collected and authorized. A copied local
cache with a new label is **not** corroboration. An externally sourced
report may not expose the same identifiers or timing; in that case this
contract **cannot** establish a match and must remain blocked rather
than inventing an account or identifying unrelated order IDs.

### Example read-only CLI

With a previously recorded session, existing `.head.json` checkpoints
and a separately collected report JSON:

```bash
lab paper-crosscheck \
  --orders results/fast/paper-orders-session-20261106-a.jsonl \
  --snapshots results/fast/paper-account-session-20261106-a.jsonl \
  --external-report separate-readonly-report.json \
  --session session-20261106-a \
  --instrument BTCUSDT-PERP.BINANCE \
  --account <exact-sandbox-account-id> \
  --max-skew-ms 1000
```

Replace the example account ID with the exact ID of the simulated
account. The command will **not** initialize missing files or
checkpoints, will not submit orders, and will not modify P&L journals.

The `--max-skew-ms` tolerance is 0–5000 ms; the default is 1000 ms.
The clocks must already be comparable. Even a zero-skew report does not
prove the two sources are independently delivered or have the same
atomic transaction boundary.

The JSON loader accepts only a preexisting, regular, at most 1 MiB
file; it rejects symbolic links, malformed UTF-8, duplicate JSON
keys (including inside nested mappings), NaN/Infinity and schema
differences.

## Checks and explicit limitations

A successful diagnostic has `status=match_unverified_source`.
It requires a fully verified source event chain and snapshot journal;
matching session, instrument, account, receipt hash/count, and latest
snapshot ID; bounded observation skew; exact signed positions;
identical outstanding-order IDs and remaining Decimal quantities;
and identical per-currency cash balances with arithmetic conservation.

All crosschecks fail closed on mismatch or incompleteness. There are
no silent numeric rounding tolerances and no force-accept switch.

**Every comparison**, including a match, returns:

```json
{
  "source_authenticated": false,
  "source_independence_established": false,
  "order_state_complete": false,
  "equity_reconciled": false,
  "margin_qualified": false,
  "risk_ready": false,
  "economic_posting_allowed": false
}
```

The report claims do not establish external account custody, cash
transfers, funding/fees, marked equity, contracts/lot multipliers,
order-state completeness, margin tiers/liquidation protection,
authenticated account identity or a profitable strategy.

## Remaining work

1. Build a separate read-only producer with trustworthy source
   identity, completeness guarantees, settlement/fee history and
   independently timestamped account snapshots. Keep API keys and
   live execution disabled until the user explicitly authorizes a
   different release.
2. Define stable field mappings between native NautilusTrader order
   IDs and the separate provider's order IDs, including
   partial fills, cancellations, corrections and restarts.
3. Add independent order-book/mark/funding evidence, margin and
   liquidation audits and operational kill-switch drills.
4. Only after a source truly qualifies, promote independently
   corroborated economics to `PaperEvidenceArchive` and
   `ProfitJournal`; continue to report daily realized net equity
   after fees, without promising daily profitability.

The test suite includes exact equality and mismatch cases, missing
or corrupted checkpoints, invalid timestamps, inconsistent orders and
currency arithmetic, duplicate JSON keys and a read-only CLI check.
Run `PYTHONPATH=src:. python -m pytest tests/test_paper_account_crosscheck.py -q`
in Debian/Termux without installing NautilusTrader.
