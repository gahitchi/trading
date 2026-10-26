# London Gold (`XAUUSD`) PAPER rollout

## Decision and current state

The operator selected IBKR **London Gold** in Commodities. IBKR documents the API
contract as `XAUUSD`, `CMDTY`, `SMART`, `USD`; the minimum order is one troy ounce.
The authenticated PAPER contract lookup on 2026-10-26 returned a USD 0.01 minimum
tick, while the general IBKR training page describes USD 0.001. The preview therefore
requires a verified tick as input. This is not a COMEX gold future and not the
`OANDA:XAUUSD` TradingView feed. Metals trading permission remains to be confirmed
for the PAPER account.

`lab.gold.gold_atr_trend_preview` is an isolated, **read-only** strategy preview. It
cannot submit an order or modify the existing stock-only paper node. Do not put
`XAUUSD` in `[universe]`: that table currently creates USD equities, assumes a US
stock session, and uses stock-share accounting without protective bracket orders.

## Requested trading rules

- Use only fully closed daily bars available at the decision time.
- Long entry candidate: SMA(50) > SMA(200), yesterday's close at/below its SMA(50),
  today's close back above SMA(50). No short position in the first version.
- ATR(14) is the arithmetic mean of true ranges on the last 14 completed daily bars.
- Maximum **position notional** is 1% of verified account equity, converted to USD
  using a current, authorized EUR/USD rate. This is not a promise that the loss is
  capped at 1% of equity. Whole-ounce quantity rounds down; under one ounce means
  abstain, never increase the allocation to satisfy the minimum.
- Indicative stop is 2 ATR below entry and the profit target is at least twice the
  entry-to-stop distance above entry (gross 2:1 reward/risk). Both levels must be
  recalculated from the **actual broker fill**, rounded to the contract's price
  increment, and linked as protective orders. Gaps, slippage, commissions and
  overnight storage can make realized reward/risk different.

## Gates before PAPER order submission

1. Read the authenticated PAPER contract details and confirm its contract ID,
   `CMDTY`, `SMART`, `USD`, minimum size, tick, permissions and usable quotes.
   A TWS search result alone does not prove API authorization. A read-only
   contract lookup succeeded on 2026-10-26, and the configured PAPER account was
   accessible; trading permission, market rules and usable quotes remain to verify.
2. Implement a metal-aware instrument, calendar, multiplier, EUR/USD conversion,
   financing/cost model and account ledger. Keep the existing US equity logic
   unchanged and preserve every PAPER-only/account/kill-switch guard.
3. Implement and test an atomic parent-with-protection order lifecycle, fill-price
   adjustment, partial fills, cancel/fill races, restart reconciliation, no orphaned
   position, stale-data rejection, and the 1% notional cap on every increase.
4. Validate closed-bar history, spreads, fees and the strategy on train/test/holdout
   data; then run supervised PAPER acceptance with small size. No profitability claim
   follows from a unit test or a visually plausible chart.

## Sources

- [IBKR commodity API contract](https://www.interactivebrokers.com/docs/tws-api/contracts/commodities)
- [IBKR London Gold contract size and permissions](https://www.interactivebrokers.com/campus/trading-lessons/trading-spot-gold-and-silver-using-ibkrs-tws/)
- [IBKR metals commissions and storage](https://www.interactivebrokers.com/en/pricing/commissions-metals.php)
