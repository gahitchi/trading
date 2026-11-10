# Binance USD-M demo account-margin cross-check v1

**Research-only offline consistency check.** New implementation:
`src/lab/binance_usdm_account_margin_audit.py`; synthetic tests in
`tests/test_binance_usdm_account_margin_audit.py`.

The existing isolated-short risk diagnostic originally received
position quantity, entry price, wallet and selected leverage as
unverified separate inputs. This adapter explicitly **compares**
a *caller-supplied demo* position-risk record against five source-pinned,
bounded-time endpoint response snapshots:

| Snapshot | Endpoint | Purpose |
|---|---|---|
| Position Information V3 | `GET /fapi/v3/positionRisk` | actual reported symbol, signed position amount, entry and mark reference, isolated wallet, UPnL and margin requirements |
| Symbol Configuration | `GET /fapi/v1/symbolConfig` | position leverage, isolated/cross margin mode, auto-add and max notional |
| Position Mode | `GET /fapi/v1/positionSide/dual` | independent one-way-mode evidence |
| Multi-Assets Mode | `GET /fapi/v1/multiAssetsMargin` | independent single-asset evidence |
| Leverage Brackets | `GET /fapi/v1/leverageBracket` | account-specific tiered maintenance ratios and cumulative adjustments |

Official documentation:
- Binance account REST API: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account
- Binance position information REST API: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade

## Input contract

A caller prepares a **single offline synthetic/demo snapshot envelope**:

```json
{
  "version": 1,
  "environment": "binance-usdm-demo",
  "symbol": "BTCUSDT",
  "collected_start_ms": 1767225780000,
  "collected_end_ms": 1767225783000,
  "sources": {
    "position": {
      "endpoint": "/fapi/v3/positionRisk",
      "captured_ms": 1767225780000,
      "payload": []
    }
  }
}
```

The abbreviated example above intentionally omits the other four
**required** endpoints and is not an executable input.

`audit_demo_account_margin(bundle, *, expected_bundle_sha256,
expected_bracket_sha256, tolerance_usdt="0.01")` requires the
complete envelope, a separately frozen whole-bundle digest and an
independently frozen original bracket-response digest. All five captures
must lie in an **at most 5-second collection window**; multiple symbols
or duplicate position records are refused, not silently selected.

The validator requires an existing one-way short (`positionAmt < 0`),
isolated collateral, auto-add disabled, USDT settlement, independently
asserted single-asset/one-way account modes, consistent position symbol,
per-tier leverage limits and maximum notional. It checks:

- Reported absolute notional vs `abs(positionAmt) × markPrice`.
- Reported unrealized P&L vs `short quantity × (entryPrice - markPrice)`.
- Reported `positionInitialMargin` vs `notional / leverage`.
- Reported `maintMargin` vs `notional × maintMarginRatio - cum`
  in the tier applicable to **current mark notional**.
- Declared isolated wallet plus unrealized P&L against computed
  maintenance; nonpositive excess has an explicit diagnostic warning.

Exact decimal arithmetic is used with an explicit bounded USDT
tolerance. The `positionRisk.updateTime` field is the **last
position-change time**, NOT the time at which the endpoint was fetched.
A quiet position can have an old update timestamp; the independent
collection timestamps are separately checked.

## No source authentication or atomicity claim

All five inputs are *user-supplied response assertions*. Pinning by
SHA-256 detects changes relative to the pinned source, **not**
exchange provenance or complete account scope. Data captured across
five separate HTTP calls is not an atomic broker state even within five
seconds. No HMAC-protected raw transport evidence, independent
order/fill history, fund transfers, or position-margin history is
supplied. The current isolated wallet **cannot be applied retroactively
at the position entry or across a historical mark-price path**.

Accordingly, the result explicitly declares
`provider_authenticated=false`, `account_state_atomic=false`,
`mark_history_verified=false`, `liquidation_qualified=false` and
`execution_authorized=false`.

The derived current-margin excess is **not** a computed Binance
liquidation price and does not authorize leveraged trading. A
mismatch blocks the report rather than being treated as evidence
of broker misbehavior.

**Never include API keys, signatures, account aliases, balances,
account exports or credentials in public Git commits or chat.**
Only fake data is stored in the committed tests. This adapter makes
no API requests, requires no credentials and places no orders.

## Acceptance

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest \
  tests/test_binance_usdm_account_margin_audit.py \
  tests/test_binance_usdm_margin_rules.py -q
```

**15 new synthetic account-margin tests** cover identical matching
position and tier requirements, current mark shifts, P&L and
maintenance discrepancies, unsupported cross/hedge/multi-asset
settings, capture windows, source hashes and credential fields,
missing/long/duplicate positions, maximum leverage, initial-margin
reconciliation, and negative maintenance excess. The prior 18
pure-margin synthetic tests should also remain green.

**Next gate:** independently authenticated demo capture workflow,
cross-export synchronization and completed one-minute mark evidence.
Only after this can we responsibly investigate an exchange-specific
liquidation state machine. This milestone does not alter the retired
BTC v1 strategy, registration, prior experiment count or protected
holdout.
