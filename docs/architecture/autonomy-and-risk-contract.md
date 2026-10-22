# Risk-adaptive autonomous trading: engineering acceptance contract

**State: agreed product objective, not yet an implemented autonomous broker capability.**
Paper-account only under `COLLABORATION.md`. The currently implemented paper node remains long-only and the CLI risk-profile output remains a research preview.

## User control and authority boundaries

Three explicit operator-selected modes are required:

| Mode | Analysis and allocation | Broker orders |
| --- | --- | --- |
| Manual | Automated research and proposals are permitted | Never submit autonomously |
| Supervised | Propose trades and an exact signed intent | Submit only after human approval for that specific intent and expiry |
| Autonomous | Independently select eligible strategies, rebalance, hedge, reduce exposure and exit when needed | May submit only inside an active, authenticated, account-specific operator mandate, hard risk limits and a fully reconciled paper broker session |

Mode activation must be durable, audited and versioned with account, strategy artifact hashes, profile ID, allowed instruments, allowed order types, allowed gross/net/short exposure, time limits, expiry and an operator-set kill switch. No research model may promote itself, increase authority or relax the profile. Expired or corrupted mandates, unknown broker state, stale prices, unverified borrow, rejected fills or unexpected margin changes force a **fail-closed** halt/abstention, without deleting positions or assuming an order was canceled.

## Adaptive strategies without unqualified model authority

Potential strategy families include validated trend/momentum, volatility targeting, mean reversion, pairs/statistical arbitrage, and market-neutral long/short books. These are **targets**, not currently qualified implementations.

- A strategy enters the selectable pool only after purged walk-forward evaluation, a held-out test, transaction costs, financing, borrow costs, delistings, sensitivity analysis and multiple-testing controls. Do not create an artificial edge by applying leverage before statistical validation.
- Market-regime selection uses observable as-of signals (volatility, correlations, liquidity, dispersion) and explicit reproducible transition rules. The model may abstain; it cannot freely invent executable code or promote new hypotheses while trading.
- Risk profiles alter *allowed techniques and exposure* as well as position/concentration/drawdown budgets. Dynamic and Aggressive may qualify for shorts and leverage; Calm/Cautious remain cash-only and long-only. Independently configured operator caps and broker house rules always take precedence.
- Leverage does not guarantee superior risk-adjusted returns and short losses can exceed the initial investment. No mode promises profits.

## Minimum testable release gates

1. In manual mode, every automated order path is blocked. In supervised mode, an expired, different, or already consumed intent cannot execute.
2. Only an explicitly opted-in autonomous mandate authorizes unattended orders. Restart retains the exact granted authority and may never upgrade it.
3. Paper account verification, persisted cash/positions/open-order/fill reconciliation and an idempotent order state machine all pass before the first autonomous order and after reconnects.
4. Known short inventory, fee provenance, margin requirements, maintenance cushion and financing are checked for every short or leveraged entry, with forced de-risking on deteriorating exposure.
5. Each strategy passes its own leakage-resistant out-of-sample and regime tests; switching must not retrospectively optimize selection on held-out outcomes.
6. An independent emergency cancel/flatten and recovery path works when ordinary data signals are stale; uncertainty about a pending order prevents duplicate opposing orders.
7. Realistic broker-sandbox tests inject missing bars, partial fills, rejects, disconnects, borrow recalls, margin changes, market gaps and simultaneous risk breaches.
8. All mode/profile transitions, reasons, approvals, settings, broker observations and trade decisions appear in an immutable, auditable journal.

**No implementation should label itself fully autonomous until these gates actually pass.** Claude is building the live sandbox harness and signed long/short core; ChatGPT's branch currently supplies risk-budget contracts and research validations for review.
