# Offline Binance USD-M isolated-short risk evidence bundle v1

**Status: research-only integration; user-environment acceptance pending.**
Committed alongside `src/lab/binance_usdm_risk_bundle.py` and 20 synthetic
tests. The frozen bearish BTC model, May 8, 2026 protected holdout,
and all existing research trial records are unchanged.

## Purpose

The existing `assess_isolated_short` model needs an account-specific
notional bracket schedule, isolated collateral, a short's entry
quantity and price, and completed one-minute **mark-price** candles.
A lone `/fapi/v3/positionRisk` response cannot establish what the
account's wallet or leverage were *at entry*. This offline integration
therefore requires snapshots at **two collection points**: immediately
after the short opens and near the assessment.

The new `validate_offline_risk_bundle(bundle, expected_sha256=...)`
connects those sources, rejects visible position/account changes,
then invokes the existing margin diagnostic. Its file-backed equivalent
is `load_offline_risk_bundle(path, expected_sha256=...)`; it is read-only,
size-capped to 2 MiB, rejects symlinks, malformed/duplicate/nonfinite
JSON and credential-bearing key names.

## Sources and envelope schema

```python
{
    "version": 1,
    "kind": "binance-usdm-isolated-short-risk-offline-v1",
    "environment": "demo",     # "mainnet" also accepted; neither authenticated
    "symbol": "BTCUSDT",
    "entry_minute_start_ms": ...,
    "assessment_ms": ...,
    "captures": {
        # One Unix ms capture timestamp per source, below
        "opening_position": ..., "current_position": ...,
        "opening_brackets": ..., "current_brackets": ...,
        "opening_config": ..., "current_config": ...,
        "opening_asset_mode": ..., "current_asset_mode": ...,
        "opening_position_mode": ..., "current_position_mode": ...,
        "marks": ...
    },
    "sources": {
        # V3 row selected by exact symbol, including symbol, positionSide,
        # positionAmt, entryPrice, markPrice, unRealizedProfit,
        # isolatedWallet, marginAsset, updateTime
        "opening_position": {...},
        "current_position": {...},
        # Complete per-symbol /fapi/v1/leverageBracket response
        "opening_brackets": {...},
        "current_brackets": {...},
        # /fapi/v1/symbolConfig response for exact symbol
        "opening_config": {...},
        "current_config": {...},
        # /fapi/v1/multiAssetsMargin GET response
        "opening_asset_mode": {"multiAssetsMargin": False},
        "current_asset_mode": {"multiAssetsMargin": False},
        # /fapi/v1/positionSide/dual GET response
        "opening_position_mode": {"dualSidePosition": False},
        "current_position_mode": {"dualSidePosition": False},
        "marks": {
            "endpoint": "/fapi/v1/markPriceKlines",
            "symbol": "BTCUSDT",
            "candles": [...] # contiguous, 12-field, completed 1m klines
        },
    },
    "costs": {
        "funding_debit_reserve": "2",   # non-negative USDT amount
        "close_fee_bps": "5",          # hypothetical adverse close fee
        "buffer_bps": "5"              # hypothetical additional buffer
    }
}
```

- Opening captures must occur in the first **60 seconds** after the
  stated aligned entry-minute start.
- Assessment must occur after the initial complete minute and no later
  than **15 minutes** after entry. Current source captures must be
  within **60 seconds** of the assessment and never in its future.
  The final mark bar must have **closed before collection**.
- Opening/current position quantity, entry price, isolated wallet and
  position updateTime must match. The account must remain **one-way,
  isolated, USDT-collateral, no auto-add margin and no multi-assets**.
  Symbol leverage and full notional bracket responses must also match.
- Opening/current V3 `markPrice` must lie inside their corresponding
  first/last mark-candle high/low. Reported unrealized P&L must be
  consistent with the short's quantity and entry/mark price.
- No ordinary last-price `/fapi/v1/klines`, incorrect endpoint/symbol,
  open/future mark candles or incomplete source is silently substituted.
- External `expected_sha256` is the canonical JSON SHA-256
  (see `source_sha256`), pinned separately from the file. This
  detects changes **after** pinning but cannot authenticate authorship.

The source JSON must remain **private and local**. Do not send signed
requests, API keys, secrets, account numbers or personally identifying
material into this repository, tests, or a chat. No network/API calls
are made by this module.

## Limits: do not promote to risk authority

- Both snapshots can agree even if unobserved account events, wallet
  movements, partial fills, rate changes, liquidation orders or
  funding settlements occurred between them. Endpoint polls are not
  atomic or a continuous user-data stream.
- Even complete 1m mark OHLC does not establish subminute sequencing.
  Fee and liquidation assumptions remain estimates. This model
  neither reconciles actual executions nor reconstructs the Binance
  liquidation engine.
- A `MARK_ENVELOPE_BREACH_POSSIBLE` result is an adverse diagnostic;
  a no-breach observation is **not proof of survival or safety**.
- Every returned assessment marks
  `source_authenticated=False`,
  `continuous_account_state_verified=False`,
  `mark_price_stream_authenticated=False`, and
  `execution_authorized=False`.
- These account snapshots are **not historical BTC strategy data**.
  Do not open the protected holdout or re-run the retired v1 strategy
  as a consequence of this integration.

## Run acceptance in Termux

```bash
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance

ruff check src tests
ruff format --check src tests

PYTHONPATH=src:. python -m pytest \
  tests/test_binance_usdm_margin_rules.py \
  tests/test_binance_usdm_risk_bundle.py -q
```

Expected **38 tests**, including 20 new synthetic bundle tests and the
previously accepted 18 pure margin-model tests.

## Official USD-M API reference

- Account: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account
- Trade: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
- Market data: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data

The account-specific signed endpoints must be accessed only through
a separately controlled and reviewed **read-only** capture process.
No collection script, credential store or live order path is included
in this milestone.
