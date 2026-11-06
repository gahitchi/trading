# Binance USD-M demo account **offline** export inspection

**Status:** provider-specific read-only schema and reconciliation diagnostics,
**not** a connected exchange account, live trading authorization or an
independent authenticated account statement.

Source module: `lab.binance_demo_export`
CLI: `lab demo-account-import --file /path/to/demo-bundle.json`
Portable tests: `tests/test_binance_demo_export.py`

No API keys, HTTP calls, private credential storage, signed requests,
exchange orders or economic journal writes are implemented here. A
separate operator-provided JSON export is required. The user has **not**
authorized connecting API credentials to the trading engine; keep this
step isolated, entirely offline and paper/demo-only.

## Public Binance documentation

The current official USD-M Futures docs name
`https://demo-fapi.binance.com` as the **testnet/demo** REST base.
The production host `fapi.binance.com` must not be mistaken for demo.

The input schema describes *responses* from read-only, signed GET
operations (no user-supplied keys in this import tool):

- `GET /fapi/v3/account` (account information V3, including
  totals, margin mode, assets and positions).
- `GET /fapi/v3/balance` (currency balances with a consistent
  account alias).
- `GET /fapi/v3/positionRisk` (all-symbol positions).
- `GET /fapi/v1/openOrders` **without symbol filter** (all open
  orders; provider indicates this request has higher weight).
- `GET /fapi/v1/income` **without symbol/incomeType filters**,
  with an explicit start/end time and at most 1000 rows.

Official references:
- https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade

These are the documented endpoint *shapes*, not proof they are
reachable with your own demo account or permitted in your region.
External fetch and true provider authentication remain untested.

## Bundle format

The exporter supplies a JSON object with exactly these keys:

```json
{
  "schema_version": 1,
  "source": "binance-usdm-demo-manual-export",
  "scope": "all-symbols-all-income-types",
  "session_id": "demo-sample-1",
  "collected_start_ns": 1800000000000000000,
  "collected_end_ns": 1800000003000000000,
  "income_start_ms": 1799996400000,
  "income_end_ms": 1800000000000,
  "account": {},
  "balances": [],
  "position_risk": [],
  "open_orders": [],
  "income": []
}
```

Empty examples above are placeholders for actual full response objects;
the validator will reject incomplete/incorrectly scoped account data.
Use Unix-epoch **nanoseconds** for local collection bounds, and
milliseconds for the requested income interval. The full acquisition
span must be at most 30 seconds and the income window shorter than one
day. The JSON source label, scope and collection timestamps are
**unverified assertions** supplied by the exporter.

Only all-symbols **USDT** USD-M perpetual positions, single-asset
margin mode and one-way `positionSide="BOTH"` are currently
supported. Unsupported hedge modes, inverse futures, multi-assets
margin and non-USDT instruments fail closed. Balances must match
the account's asset wallet balances exactly, and the full nonzero
position map must agree across account/positionRisk responses.

Open orders are checked for client ID uniqueness, recognized
nonterminal states and exact positive remaining quantity:
`origQty - executedQty`. Cash, position and income amounts are
strict decimal strings, never binary floats. Arithmetic for
wallet + unrealized P&L = margin balance, order remaining
quantity and per-income-type totals uses an explicit high-precision
Decimal context: **Python's default 28-digit precision is insufficient
for the permitted 25-digit integer and 18-digit fractional fields**.
Regression tests establish that microamounts are neither silently
lost nor fabricated as matching margins alongside large balances.
These extreme-value tests check parser arithmetic, not a claim
that a real demo wallet holds such a balance.

Income history must use its full unfiltered type coverage and
strict start/end window, with unique `(incomeType, tranId)` pairs
and signed currency-specific amounts. A result at the 1000-row cap
is refused as potentially truncated. An empty response does not
prove the provider has returned every settlement in the period.
Other income types (transfers, rebates, bonuses and corrections)
are preserved by type and currency: they must not silently become
trading profit or funding.

Import is **read-only**. The local JSON must be regular, no
symlinks, no larger than 4 MiB, and must have strict UTF-8 JSON,
no duplicate keys, unbounded decimals, NaN or Infinity. Keys
containing API credentials or signatures are rejected. Source
hashes are reported for audit, not provider authentication.

### Local usage

```bash
PYTHONPATH=src:. python -m lab.cli demo-account-import \
  --file /path/to/private-demo-bundle.json
```

The tool prints normalized positions, currency wallet balances,
outstanding order remaining quantities and income claim totals.
It **does not** write another file. Your account figures stay on
the machine where you run it; never paste API credentials or
private reports into a chat or commit them to GitHub. The repository
ignores `results/` and `data/` but user-managed external files
may need separate protection.

Because Binance demo and the **local NautilusTrader sandbox**
are **different accounts**, the output explicitly includes:

```json
{
  "provider_identity_authenticated": false,
  "independent_collection_verified": false,
  "complete_income_coverage_verified": false,
  "account_state_atomic": false,
  "nautilus_simulator_account_matched": false,
  "equity_reconciled": false,
  "margin_qualified": false,
  "risk_ready": false,
  "economic_posting_allowed": false
}
```

Neither favorable cash balances nor matching trade IDs constitute
independent proof the two accounts executed the same orders.
A future authorized adapter would need authenticated read-only
source collection, clock and request/response envelopes, consistent
venue account identifiers, pagination, websocket reconciliation
and independent custody of captured responses. No such adapter is
connected yet.

**No live or demo trade order placement is enabled by this task.**
