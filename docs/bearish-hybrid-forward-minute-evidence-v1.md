# Bearish hybrid — forward UTC minute evidence, research-only v1

**Only read-only Binance USD-M public GETs. No keys, balances, orders, executable signals, filled trades, daily-profit claims, or real funds.** This draft is stacked onto PR #44 and makes no changes to `main` or earlier collaborator branches.

## Accepted initial external evidence (2026-11-16)

The independent user-operated Termux host captured five immutable ETHUSDT open-interest HTTP receipts. The first three were valid and fresh but too close together to meet the preregistered 5–60 minute measured-time and receipt-time separation. The following two measurements (six-minute collection interval) produced a valid OI comparison: **-0.021501221604502962%**. This is a small OI decline, **not** a price direction signal, verified Binance attestation, or demonstrated edge. Neither the public endpoint nor local capture clocks provide independent signatures. The corresponding raw evidence remains on the user's host; this repository does not contain, fabricate, or reproduce those capture bytes.

## Additive forward price recorder

`lab.bearish_hybrid_forward_price.record_forward_price` fetches only the constructed public `/fapi/v1/klines?symbol=<SYMBOL>&interval=1m&limit=2` endpoint for a validated non-BTC USD-M USDT symbol. The response is bounded to 8192 bytes; redirects are rejected by the reused HTTPS fetcher. Request and response-completed UTC nanoseconds, original response bytes, SHA-256 and an explicitly unverified source label are saved immutably, one receipt file per receive timestamp.

Exactly one or two ordered consecutive 1-minute Binance kline rows may be supplied. The parser rejects invalid numeric values, candle timestamps, unaligned/gapped rows, impossible OHLC or taker volumes, and anything that is not a minute **closed before the request began**. The selected minute must be at most two minutes old on receipt. The current, still-open candle is never accepted. `load_forward_prices` verifies source bytes, schema, hashes and monotonically received receipts offline, rejecting a revision to a previously recorded completed candle. Duplicate identical bar observations are permitted but do not count as different minutes. **Missing bars remain missing**.

Stored path: `<root>/binance-usdm/forward-prices/<SYMBOL>/<received_ns>.json`.

The stored flags `independently_authenticated`, `historical_publication_verified`, `price_history_complete`, `qualified` are all **false**, and `actual_notional=0`. A saved price receipt cannot be promoted automatically to a funded `HybridObservation`.

## Bounded public collector

`lab.bearish_hybrid_forward_collector` samples **future UTC minutes** at approximately five seconds after each minute boundary:

- one price receipt on each sampled minute;
- one open-interest receipt every sixth sampled minute (indices 0, 6, 12, etc.);
- settled-funding history at the start and once per 60 sampled minutes.

The three requests are sequential, **not atomic or exchange-synchronized snapshots**. Failure aborts rather than synthesizing missing receipts. The collector only runs for an explicit `--minutes` value from 1 to 120. It does not daemonize or silently recover gaps. If Termux is suspended or endpoints fail, there may be missing or duplicate minutes; gaps are reported and never backfilled. The source stores only locally received availability, not assumed historical publication times. OS clock integrity and actual venue identity are **not** independently attested.

## Independent local acceptance on Termux

```bash
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/bearish-forward-minute-evidence-20261116

PYTHONPATH=src:. python -m pytest -q \
  tests/test_bearish_hybrid_public_capture.py \
  tests/test_bearish_hybrid_forward_gate.py \
  tests/test_bearish_hybrid_forward_price.py

PYTHONPATH=src:. python -m lab.bearish_hybrid_forward_collector \
  --root ~/trading-forward-evidence \
  --symbol ETHUSDT --minutes 12
```

Afterward, inspect recorded forward prices **offline**, without exposing keys or uploading sensitive account data:

```bash
PYTHONPATH=src:. python - <<'PY'
from pathlib import Path
from lab.bearish_hybrid_forward_price import load_forward_prices

records = load_forward_prices(Path.home() / "trading-forward-evidence", "ETHUSDT")
print("Stored price receipts:", len(records))
print("Distinct completed bars:", len({r.bar.end_ns for r in records}))
print("Last locally received:", records[-1].received_ns if records else None)
print("Source attestation independently verified: False")
print("Eligible for trading: False")
PY
```

## Outstanding before strategy qualification

Independent source and clock attestation; continuous prospectively covered price/funding/OI observations with auditable outages; funding-history completeness; spot/perp listing and as-of 30-day turnover; price/BBO and depth latency; realistic adverse spread and slippage; margin/liquidation, settlement and wallet evidence; registered negative controls and properly protected holdout analysis. Historical or prospective data collection alone cannot establish positive expectancy or daily profits. The bearish short-only engine must stand aside without independently validated conditions.
