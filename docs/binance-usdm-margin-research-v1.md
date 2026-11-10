# Binance USD-M isolated short margin diagnostic v1 (2026-11-10)

**Research-only, unqualified and completely offline.** New files:
- `src/lab/binance_usdm_margin_rules.py`
- `tests/test_binance_usdm_margin_rules.py`

This is **not** an exchange liquidation engine, position controller,
execution approval, or profitable-strategy validation. Existing frozen BTC
v1 and its pre-May-8-2026 research evidence are unchanged.

## Why this model needs separate Binance data

Binance USD-M liquidation uses **mark price**, not last-traded kline
prices. The notional tier determines the maintenance rate and its
cumulative amount, with the general schedule expression:

```
maintenance = abs(position_quantity) * mark_price * tier.maintMarginRatio - tier.cum
```

The tier is found from the **current mark notional** and must be
reselected at every tier crossing. The source `initialLeverage` also
limits acceptable entry leverage. The bracket response is USER_DATA,
not an anonymous public BTC rule. Its `notionalCoef` can be
account-specific and is **rejected unless 1** until verified support
for modified accounts exists.

**Official references:**
- Binance liquidation methodology:
  https://www.binance.com/en/support/faq/detail/360033525271
- Maintenance/cum and tier-recalculation examples:
  https://www.binance.com/en-IN/support/faq/detail/b3c689c1f50a44cabb3a84e663b81d93
- USD-M mark-price candles:
  https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- Binance official connector's leverage-bracket USER_DATA endpoint:
  https://github.com/binance/binance-futures-connector-python/wiki/USDT-M-Futures

The raw source must be supplied independently, normally from
`GET /fapi/v1/leverageBracket` with authorization. **Never place
API credentials in the repository, test fixtures, chat or documents.**
This module makes no API calls and does not need credentials.

## Constraints and safety gates

`parse_leverage_brackets` validates the complete, pinned per-symbol
bracket response and requires:
- exact SHA-256 of canonical JSON, exact `BTCUSDT` or another explicit
  USDT symbol, ordered tiers starting at zero, contiguous floor/cap,
  decreasing maximum leverage, nondecreasing maintenance rate, and
  consistent `cum` across boundaries
- a complete, externally pinned raw mark-candle envelope with
  `endpoint="/fapi/v1/markPriceKlines"`, the matching symbol and
  12-field documented Binance kline tuples, with every minute and
  close timestamp in sequence; **ordinary /fapi/v1/klines are not accepted**
- recent bracket capture relative to the assessment (at most 60 s),
  and a complete recent mark-minute candle (within 60 s)
- only **one-way (BOTH), isolated, single-asset USDT** collateral
  from the input; crossed, hedge and multi-assets modes are rejected
- explicit short quantity, *executed* entry fill, isolated wallet after
  entry, selected leverage, adverse funding debit reserve,
  exit-fee estimate and additional safety buffer
- sufficient initial collateral and a tier-supported selected
  leverage at entry and at both stressed and current notional;
  attempts beyond pinned tier caps are rejected

Both sources are integrity-pinned but **NOT authenticated merely by
SHA-256**. An external capture provenance and account reconciliation
must independently establish whether the data genuinely came from
the exchange and the correct user account.

### Diagnostic formulas

For one isolated USDT short, with quantity `q > 0`, entry fill
`E`, observed mark `P`, isolated entry-time wallet `W`,
unpaid adverse funding liability reserve `F >= 0`, maintenance tier
`r, cum` at `q * P` and adverse fee/buffer fraction `c`:

```
isolated_margin_balance(P) = W - F + q * (E - P)
maintenance(P)           = (q * P) * r - cum
modeled_excess(P)        = isolated_margin_balance(P) - maintenance(P) - q * P * c
tier_root                = (W - F + q * E + cum) / (q * (1 + r + c))
```

The module searches all tiers for a **unique root that actually lies
inside the assumed tier**. Failure to establish one root stops the
calculation. It compares both current mark and **highest observed
mark** against maintenance. A risk breach is reported as
`MARK_ENVELOPE_BREACH_POSSIBLE`; otherwise the result is
`NO_MARK_ENVELOPE_BREACH_OBSERVED_UNQUALIFIED`.

Neither outcome asserts exactly when Binance would liquidate, what
fill a liquidation would obtain, that the account survived, or that
risk management can approve a live order. Fee/buffer assumptions are
not sourced from actual account fee tiers here. Binance may partially
liquidate, cancel orders, use additional collateral or apply other
contract-specific rules. This version never simulates such actions.

**Temporal limitation:** A recently captured bracket schedule must
not be applied to years of archived prices as though the risk tiers
were unchanged. The model is a point-in-time research diagnostic,
not a substitute for historical source-pinned bracket snapshots.
A full one-minute high series still cannot prove exact subminute
liquidation ordering or account actions.

## Synthetic acceptance

```bash
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest \
  tests/test_binance_usdm_margin_rules.py -q
```

The 18 synthetic tests cover tiered maintenance and `cum` transitions,
mark-price spike/recovery, unique root, bracket hash/symbol/coefficient,
isolation/hedge restrictions, leverage and initial margin, minute
coverage, wrong market-data endpoint, stale/future evidence, source
mutation, over-cap positions and invalid fee/funding liabilities.
The local sandbox checks passed; Termux acceptance still required.

## Remaining before actual risk authority

1. Independently pin and authenticate account-specific source responses
   without exposing credentials, and distinguish demo from mainnet.
2. Integrate broker wallet, order/income event streams, open orders,
   partial fills and leverage changes with timestamped account state.
3. Build a tiered liquidation-state machine that can handle partial
   liquidation, clearance fees, ADL and mark-price stream gaps.
4. Require separate research governance for any new strategy or
   account data: **do not rerun or repurpose the retired BTC v1
   experiment or open its protected holdout**.
