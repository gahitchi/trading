# Two-export Binance demo wallet discrepancy audit (offline)

Module: `lab.binance_demo_wallet_audit`. CLI: `lab demo-wallet-audit`.
Status: **research-only diagnostics with unverified input provenance**.

This compares *two existing, locally exported* Binance USD-M demo account
bundles, using the strict JSON importer documented in
[offline demo export](binance-demo-export.md). No API keys, HTTP,
order endpoints, economic journal writes, or live exchange access exist.

Run:

```bash
PYTHONPATH=src:. python -m lab.cli demo-wallet-audit \
  --before /path/to/earlier-demo-export.json \
  --after /path/to/later-demo-export.json
```

The tool requires the same claimed session and account alias, distinct
input digests and chronological **nonoverlapping** collection windows.
The later export must claim an income history interval that covers the
gap from the **end of the earlier collection** to the **start of the
later collection**. The conservative comparison never attributes an
income event timestamped inside either non-atomic collection window.
Conflicting transaction IDs reused in overlapping income histories are
rejected; earlier historical rows are not double counted.

All wallet assets must appear in both exports; a missing asset cannot
be assumed zero. Monetary amounts are compared with high-precision
Decimal arithmetic, retaining each currency separately.

Output includes the per-asset wallet delta, claimed income movement,
and **unexplained residual**:

`residual = observed wallet delta - sum(claimed income in gap)`.

A zero residual is tagged `wallet_delta_consistent_unverified`. A
nonzero residual is tagged `wallet_delta_unexplained`; it is *not*
manufactured as profit, a funding payment or a transfer. Unknown income
types are reported rather than silently classified as trading profit.

These are *separately collected, signed-integer timestamps from
unverified files*. Neither file authenticates the exchange, proves
pagination completeness, reconciles actual custody, nor establishes
atomic account balances. Wallets can change during the collection
windows and other types of account adjustments may be missing.

**Every result** has `actual_cash_settlements_verified=false`,
`income_coverage_independently_verified=false`,
`equity_reconciled=false`, `risk_ready=false` and
`economic_posting_allowed=false`.

Portable tests:

```bash
PYTHONPATH=src:. python -m pytest tests/test_binance_demo_wallet_audit.py -q
```

Next: independently authenticated and complete demo-account source
capture, trustworthy income pagination and source clock consistency,
margin/contract metadata, fill/fee reconciliation and auditable daily
net equity. Do not reuse Nautilus simulator account IDs as if they
were Binance demo account identities.
