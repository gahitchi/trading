# Bearish hybrid — forward-observed public feature receipts (research-only v1)

This extension of draft PR #42 adds an **append-only public REST capture** of open interest and settled funding rate history for Binance USDⓈ-M USDT perps. It does not download protected BTC research data, access an exchange account, place paper/live orders, or claim a profitable strategy.

## The key time distinction

- The USD-M `GET /fapi/v1/openInterest?symbol=...` response includes exchange-reported `time` (a measurement timestamp). That is **not evidence that the result was published/received then**. We separately record the UTC clock immediately **before issuing** the request and **after the final response byte** was received. Only the latter is used to make the snapshot available to a simulated decision. Future or inconsistent measurement times are rejected.
- The USD-M `GET /fapi/v1/fundingRate?symbol=...&limit=100` records carry `fundingTime` (settlement), not a verified publication timestamp. Past settlements retrieved today cannot be used in an earlier historical simulation. The first **locally received** observation is a conservative `published_ns` proxy for the existing `FundingEvent` contract, **not** a venue publication attestation. Later corrections to an already observed settlement cause a fail-closed error.
- Neither endpoint establishes full funding coverage, contemporaneous listing/30-day turnover, rate applicable to actual portfolio cash, mark-to-market margin, or proof of response authenticity independent of the local HTTPS client. Every receipt sets `independently_authenticated=False`, `historical_publication_verified=False`, and `funding_history_complete=False`. **Never turn those flags true merely because files have hashes.** There is no automatic `funding_verified=True` bridge to the hybrid signal engine.

Official reference endpoints: <https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest> and <https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History>. Server API availability, network access, regional access, and schemas may change. This project does not verify them by live HTTP until the user explicitly runs the collector.

## Capture and revalidation

This module is **not** scheduled automatically; each invocation makes at most one request to its fixed public endpoint and stores one self-contained receipt containing the raw base64 response bytes, SHA-256 and UTC request/receipt timestamps. There are no API keys or trading endpoints. The local collection clock is not an independent trusted time authority, and a locally mutable receipt is not a signature. Bounded reads, validated symbol and payload, and exclusive-create files mitigate accidental misuse and silent overwrites.

Example: start building forward receipts on the user's Termux machine, *outside Git*, without reading or replaying historical holdout files:

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
PYTHONPATH=src:. python - <<'PY'
from pathlib import Path
from lab.bearish_hybrid_public_capture import record_public_feature
root = Path.home() / "hybrid-public-captures"  # do not commit captured data into Git
for kind in ("open-interest", "settled-funding"):
    receipt = record_public_feature(root, "ETHUSDT", kind)
    print(kind, receipt.received_ns, receipt.raw_sha256, receipt.source)
PY
```

Each call stores under `hybrid-public-captures/binance-usdm/forward-features/ETHUSDT/<kind>/<received_ns>.json`. Public REST requests may be rate-limited or geographically unavailable; never introduce unbounded or aggressive polling. Replay only pre-authorized, preregistered, independently reviewed research windows, and never use the protected BTC historical holdout without its separate explicit protocol. Re-check captured bytes off-line via `load_public_features(root, symbol, kind)`.

`first_seen_oi(oi_captures, decision_ns=bar_end)` builds an `OIObservation` only when two previously received snapshots were available, five to sixty minutes apart, and the newest is fresh within 60 minutes. `first_seen_funding(funding_captures, decision_ns=bar_end)` returns at most the trailing 72-hour settled events **first seen by that completed bar end**. Neither function certifies trading eligibility or actual trading profitability. Funding rates and OI readings may be revised, and a local clock can be skewed.

## Remaining gates

1. Collect an independent, prospective source trail with trustworthy clock synchronization and external auditing; original daily price archives from PR #42 alone do not show historical receipt times.
2. Establish verified contemporaneous funding completeness and as-of market eligibility, rather than inferring them from today's REST response or a historical snapshot's nominal timestamp.
3. Pre-register the entire study family and all reinvestment fractions before looking at returns; preserve source arrival and parameter hashes, count even failing runs.
4. Measure realistic latency/order-book fills, exchange margin and funding cash, loss distributions and independently protected out-of-sample periods before claiming a repeatable edge.

Tests use only synthetic responses; run `PYTHONPATH=src:. python -m pytest tests/test_bearish_hybrid_public_capture.py -q`, Ruff checks, and the complete suite. No current historical performance is established.
