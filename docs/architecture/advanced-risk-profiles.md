# Short selling and leverage — explicitly gated research design

**Status:** offline mathematical stress prototypes only; **no automated short or margin orders.** This design extends the five selectable risk styles without weakening the paper-only account guard, broker recovery or safety stop. Broker-specific financing, locate, margin and liquidation functionality is still unimplemented and unqualified.

## Profile semantics

| Profile | Long budget | Simulated shorts | Simulated leverage | Paper short/margin order permissions |
| --- | --- | --- | --- | --- |
| Calm | Lowest | No | No | Disabled |
| Cautious | Low | No | No | Disabled |
| Balanced | Medium | Educational shadow preview only | No | Disabled |
| Dynamic | Higher | Eligible for **separate** research authorization | Eligible for **separate** research authorization | Disabled |
| Aggressive | Highest within absolute caps | Eligible for **separate** research authorization | Eligible for **separate** research authorization | Disabled |

The operator's tighter configurable limits always win. Profile selection **never** grants broker privileges or overrides an active safety halt. Current `lab risk-profile --profile <name>` returns a preview and fingerprint, not a deployment change.

The actual profile model now has explicit research limits for signed net exposure and short gross. Its gross ceilings are Calm **0.25×**, Cautious **0.40×**, Balanced **0.75×**, Dynamic **1.35×**, and Aggressive **1.80×**. Dynamic can have up to **0.30×** short gross and Aggressive **0.75×**, but only when the operator independently configures equally permissive hard limits. The checked-in default is `max_gross=1.0` and `max_short_gross=0.0`, hence neither margin nor short research is silently enabled.

`[risk] profile` is the saved preference for `lab risk-profile` previews. **It is not wired to the paper broker.** When core signed accounting and qualification tests are integrated, the live execution path must consume one persisted effective profile, bind it to the account and approved strategy, and fail closed on mismatch. Profile target volatility is no longer silently disabled by the original zero/unspecified portfolio target in research previews. To become selectable for unattended **paper** orders, risk-profile activations must be persistently bound to account, approved strategy artifacts, versioned risk policy, and broker reconciliation evidence. No real-money account support is in scope.

## Expert short/leveraged portfolio reasoning

A responsible margin bot must reason about:

- **Borrow reality:** instrument eligibility, hard-to-borrow fees, actual short locate, availability expiry, lender recalls, buy-in risk and applicable local short restrictions.
- **Collateral and financing:** cash/stock collateral classification, margin loan principal, interest accrual, opening/maintenance requirements, settled versus unsettled cash, restricted short sale proceeds and broker house margin add-ons.
- **Unlimited short exposure:** a short's loss can exceed initial capital if price rises without bound; conventional stop-losses and a finite set of stress scenarios do **not** cap gap or forced-buy-in risk.
- **Portfolio interaction:** gross and net exposure, long/short concentration, market-neutral exposures, foreign-currency liabilities, beta and factor shocks, correlated squeezes and changing margin haircuts.
- **Dynamic safety:** real-time broker equity and available funds; stale quote and borrowing halt; forecast abstention; order/fill idempotency; persistent total exposure and borrowing; liquidation controls independent of daily signals.

Research prototype `src/lab/advanced_risk.py` models a single reporting-currency long/short position book. Explicit signed quantities, bid/ask marks, documented margin loan principal, known borrowing and financing rates, and stress returns produce portfolio equity, gross leverage, short liability/equity, estimated holding-period costs, stressed equity, margin cushion breaches and risk-limit reasons. Inputs are finite `Decimal`, duplicate/unknown instruments and missing borrow evidence fail closed. Even when no hypothetical limit is breached, output `can_submit_orders: false` remains fixed.

**Important limitations:** this prototype is *not* IBKR's risk engine, a short-locate query, verified borrow availability, margin requirements by asset or exchange, actual settlement, portfolio stress VAR, portfolio beta hedge optimizer, live futures/derivatives netting or an execution adapter. It is deliberately prohibited from placing any orders.

## User workflow

1. Choose a profile in research: `lab risk-profile --profile dynamic --json` and review the conservative hard caps. Understand that this has not changed the actual paper node.
2. Construct a hypothetical long/short portfolio with signed quantities, accurate current bid/ask, supplied financing and borrowing assumptions. Label provenance on each.
3. Evaluate severe upside squeezes and downside market gaps across multiple correlated scenarios using the isolated risk engine.
4. Review breach reasons. A strategy requiring missing locate/finance information or producing material margin risk is **not eligible** for approval.
5. AI research workers may generate hypotheses and proposed orders, but only authenticated operator authorization and a fully reconciled, independently tested paper broker path could eventually enable an order. Never infer permission from a model score.

## Qualification gates before real paper deployment

- Instrument-specific short permissions, real broker shortability and locate freshness, borrow fee/slippage data, and correct currency/venue settlement.
- Separate margin policy by security, account type and broker house rules, including margin calls, margin loan accrual and forced liquidation.
- Crash-safe audited outbox, reconciled broker cash+collateral+pending orders+fills+corporate actions, account-scoped persistent locks and dynamic risk controls.
- Chaos testing on buy-ins, short recall, rejected locates, overfills, adverse squeezes, FX funding gaps, margin-call timing and emergency halt/flatten.
- Shadow soak, supervised paper qualification, explicit human review and immutable versioned approval; no automatic profile escalation from profit.

**Do not let a higher-risk profile turn uncertainty into authority.**


## IBKR borrowing information that the adapter must distinguish

Official sources: [IBKR short-securities availability](https://www.interactivebrokers.com/en/trading/short-securities-availability.php), [TWS streaming market data, generic tick 236](https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data).

- TWS generic tick **236** exposes shortability tier (tick 46) and estimated shares available (tick 89). It does **not** itself certify an executable locate for a particular order.
- IBKR's Stock Loan/Borrow (SLB) facility separately displays indicative available quantity and borrow-fee rates, with historical indicative borrowing-rate data. Availability and rates can change; the broker still decides whether an individual order can short.
- A short research position must provide an as-of timestamp, permanent instrument identity, availability source and explicit annualized fee. **Missing, stale, or non-point-in-time borrow history excludes that asset from a qualified historical short backtest.** It is not acceptable to replace missing hard-to-borrow costs with zero.
- Daily research carry should charge short market value × supplied annual borrow fee × elapsed days / 365 and charge actual financing on account debit balances; collateral interest, short-sale proceeds, dividends and corporate actions need separate explicit cash flows. The broker's actual fees and house margin rules supersede indicative calculations.
- Approval must use broker-confirmed account permissions, current locate/order validation where required, account-specific buying power and initial/maintenance requirements. An account name, risk profile or the 2× heuristic is not proof of margin eligibility. An unverified order is rejected or abstained from, never silently converted into a different side/quantity.

**Gate remains closed:** `advanced_risk.py` is a deterministic research calculator; no verified borrow feed, qualified core position accounting, or broker-approved short/margin order path has been implemented.
