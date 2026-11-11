# Binance USD-M offline user-data event consistency v1

**Research-only and unqualified.** The new `src/lab/binance_usdm_user_stream_audit.py`
validates a caller-supplied bounded sequence of observed Binance USD-M
user-stream messages against opening/closing **comparison** snapshots.
It does not fetch data, open sockets, store API keys or execute orders.

The model intentionally refuses to certify websocket completeness:
**Binance user-data messages do not provide a global gap-free message
sequence ID.** A stream that appears connected and has matching snapshots
can still miss events between those snapshots. The evidence also lacks an
authenticated provider channel, atomic positions or full execution income.

## Input contract

```python
{
    "version": 1,
    "environment": "binance-usdm-demo", # or binance-usdm-mainnet
    "symbol": "BTCUSDT",
    "start_ms": 1767225600000,
    "end_ms": 1767225720000,
    "opening": {
        "symbol": "BTCUSDT",
        "wallet_balance": "300",
        "position_amt": "-2"
    },
    "closing": {
        "symbol": "BTCUSDT",
        "wallet_balance": "298",
        "position_amt": "-2"
    },
    "stream": {
        "connected_ms": 1767225599000,
        "disconnected_ms": 1767225721000,
        "reconnects": 0,
        "listen_key_expired": False,
        "message_capture_complete": True
    },
    "events": [
        {"received_ms": ..., "payload": {
            "e": "ACCOUNT_UPDATE", "E": ..., "T": ...,
            "a": {"m": "FUNDING_FEE", "B": [
                {"a": "USDT", "wb": "298", "cw": "0", "bc": "-2"}
            ], "P": [
                {"s": "BTCUSDT", "pa": "-2", "ep": "600",
                 "cr": "0", "up": "0", "mt": "isolated",
                 "iw": "300", "ps": "BOTH"}
            ]}
        }}
    ]
}
```

Call `audit_user_stream(bundle, expected_sha256=source_sha256(bundle))` for
synthetic or locally constructed data. **For a real audit, pin the
expected digest separately BEFORE any data changes**. A hash guards
integrity of the supplied record, not its exchange provenance.

The event parser verifies each observed USDT wallet balance-change amount against the prior observed wallet, and rejects even temporary long inventory. It supports the bounded subset `ACCOUNT_UPDATE` and
`ORDER_TRADE_UPDATE`, with strict event/transaction/receive timestamp
checks, isolated one-way position identity, and fixed USDT wallet
updates. It refuses unknown reasons/events, credential-like fields,
contradictory snapshot quantities, duplicate trade IDs, regressing
cumulative fills, overlapping/expired capture claims, nonfinite data,
and missing required fields.

### Conservatism

- Sparse `ACCOUNT_UPDATE` B/P arrays are treated as **no relevant
  observed change** for absent items; absence is **not proof** that the
  asset or position never changed elsewhere.
- `ORDER_TRADE_UPDATE` reports observed fills/order-state updates.
  No wallet PnL, commission, isolated collateral change, or quantity
  movement is inferred from an order event alone. The report exposes
  `unresolved_order_updates` and does not reconcile executions as
  authenticated settlements.
- A gap-free local capture flag is a **user-supplied claim**, not proof
  of gap-free delivery. The module cannot recognize every packet loss
  or disconnected period just from event counters.
- Consequently `websocket_delivery_complete`,
  `execution_history_complete`, `provider_authenticated`,
  `risk_ready` and `execution_authorized` are ALWAYS `False`.
- This validator is not connected to historical BTC ensemble v1,
  does not open its protected holdout, and cannot certify profitability.

Official references:
- https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Balance-and-Position-Update
- https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Order-Update
- https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Connect

## Termux acceptance

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest \
  tests/test_binance_usdm_margin_rules.py \
  tests/test_binance_usdm_risk_bundle.py \
  tests/test_binance_usdm_account_margin_audit.py \
  tests/test_binance_usdm_user_stream_audit.py -q
```

The three earlier risk suites were accepted in Termux (53 tests).
The 17 new synthetic user-data audit tests require acceptance there.
A passing audit establishes observed internal consistency only.
