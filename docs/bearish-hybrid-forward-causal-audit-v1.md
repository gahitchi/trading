# Bearish hybrid — offline forward as-of evidence audit (research only)

This is stacked on the separately accepted public collector PR #45, and uses only previously saved immutable receipts. It does not send public or private network requests. There are no keys, orders, executable notional, reconstructed fills or historical/prospective P&L.

## Independent host acceptance already observed

On 2026-11-16 the Termux host reported:

- public Binance USD-M `/fapi/v1/klines` and `/fapi/v1/openInterest`: HTTP 200 with successful TLS verification;
- the post-TLS-failure collector regression suite: **26 passing tests**;
- `--minutes 3` produced three consecutive ETHUSDT closed-price bars, each 60 seconds apart, and no transport failures;
- one open-interest and one settled-funding receipt were collected in the first of the three poll cycles.

Original response bytes remain on the user-operated host; GitHub does not hold those bytes and cannot independently attest them. Three minutes of collection is not enough to establish sustained data completeness, profitability, or an independently verified source.

## First-received clock and anti-leakage rule

The original OHLC bar's `end_ns` marks the exchange-reported candle boundary, **not when the local recorder learned its contents**. The audit assigns the first diagnostic `decision_ns` at the next aligned UTC minute boundary **not earlier** than the price's stored HTTP `received_ns`. OI and funding can be used in that diagnostic row only when their respective original receipt-completion timestamps are <= that decision boundary. Later receipts, including funding backfills with old settlement times, cannot be used to revise an earlier as-of decision.

The price/OI/funding HTTP requests are separate and not an atomic venue snapshot. The collected observations may be delayed. This policy gives a conservative research as-of timestamp, **not proof that orders could have executed at the candle close**.

The offline `audit_forward_evidence(root, symbol)`:

- verifies source receipt schemas and hashes via the existing independent price / OI / funding load functions;
- collapses identical repeats of the same closed-minute bar into one first-received observation, counting duplicate receipts explicitly;
- counts missing **internal** minutes between received distinct bars, without inventing pre-collection or post-collection coverage;
- uses `first_seen_oi` (distinct measurement time and 5–60m received-and-measured separation) and `first_seen_funding` (no time-travel of settled rates);
- reports as-of receipt counts, available OI percent change if genuinely supported, and count of known funding settlements;
- **never** sets `funding_verified`, `market_eligibility_verified`, `price_history_complete`, `source_independently_authenticated` or `qualified` to true; executable notional remains zero.

An OI decrease does not identify whether traders initiated new short positions or closed existing longs. Even an as-of complete set of OI, price and funding readings is not evidence for a profitable, executable bearish setup.

## Termux acceptance

```bash
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/bearish-forward-causal-audit-20261116

PYTHONPATH=src:. python -m pytest -q \
  tests/test_bearish_hybrid_forward_price.py \
  tests/test_bearish_hybrid_forward_audit.py

PYTHONPATH=src:. python -m lab.bearish_hybrid_forward_audit \
  --root ~/trading-forward-evidence \
  --symbol ETHUSDT \
  --last 12
```

The audit is strictly offline; if evidence from the earlier three-poll collection has not been deleted, it should show three distinct bars and zero internal missing minutes. OI and funding counts can be higher than one because the user's evidence root contains the previous five OI and initial funding receipts.

The independent local output is not yet available in the GitHub PR; CI uses synthetic-only receipts. Additional uninterrupted forward capture, explicit capture-interval coverage, observability of failures, source/time provenance, as-of listing and 30-day turnover, exchange funding verification, L2 fill/latency evidence, and independently protected preregistered market research are still necessary before any profitability claim or paper execution qualification.
