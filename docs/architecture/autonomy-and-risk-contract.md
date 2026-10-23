# Autonomous operation and risk-control contract

**Owner decision (2026-10-22): autonomous, uninterrupted paper trading is the default operating model; manual and supervised modes are desired alternatives.** An operator explicitly starts `lab paper` for unattended paper trading. There is no automatic real-money authority.

What exists today: deterministic signal agents, volatility allocation, risk filters, broker paper-account guard, restart reconciliation, hard/transient halt classes, live-node harness, and the new risk-unit profile setting. A selected risk profile changes the *actual* numeric limits consumed by the agents; it does not grant a short locate, relax a broker margin rule, waive a breaker or certify statistical edge. Automatic repair of transient faults must rerun account, price and broker-state checks. Unexplained or ambiguous journal orders are hard faults.

Implemented on the ChatGPT release candidate: local identity-bound expiring authority,
manual targets, exact single-use supervised proposals, and an opt-in daily bank with
predeclared regime eligibility, expiry, durable exception retirement and cash fallback.
See [operator controls](../operator-controls.md). The bank remains experimental pending
whole-policy out-of-sample qualification. Local HMAC controls authenticate the OS user,
not an external identity provider. Paper startup remains autonomous by default; an
explicit mandate bounds its duration and a supervisor restart cannot renew it.

External qualification still required: actual broker borrow inventory, IBKR routing
and house-margin semantics, real paper lifecycle evidence, whole-policy statistical
validation, deployment soak and independent security/code review.

Acceptance gates: no real-money accounts; no order without reconciled broker and a confirmed completed market session; signed gross/net exposure plus maintenance cushion; a durable journal preventing unknown-order duplicates; hard halts cannot silently auto-reset; paper-only emergency flatten; verified borrow evidence before short research eligibility; correct approval/expiry handling before any supervised mode; and rigorous out-of-sample multiple-trial statistical tests before strategy promotion.

The current UI is CLI. Run `lab risk-profile --json` to inspect the operative limits. `[risk] profile = "custom"` preserves existing behavior until the operator explicitly chooses a preset. Selecting a preset is not switching between automated/manual authority modes.

Historical backtests now optionally enforce dated borrow evidence and per-day fees via `[borrow].history`; see `borrow-evidence.md`. This does not qualify broker shortability or add an autonomous strategy selector.
