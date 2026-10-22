# Unattended research hardening (first pass)

This branch is a **first safety patch, not certification for unattended trading**.
The system remains PAPER-ONLY and requires further integration and fault-injection tests.

## Changes
- Persist a strict, fsync-backed drawdown high-water mark and sticky halt across restarts.
  Corrupt risk state forces a trading halt; only a deliberate operator recovery should restore it.
- Fail closed if any expected strategy sleeve has not produced today's signal and risk
  approval by the rebalance decision.
- Journal order intent **before** submission. Unknown intents after a restart block trading
  until broker order reconciliation. Persist daily order and buy-turnover usage.
- Use durable atomic JSON checkpoints for both risk and execution.
- Prevent backtest/null/PBO custom date ranges from entering the sealed holdout.
- Add regression tests for checkpoint corruption and holdout bypass.

## Outstanding P0 work (required before unattended deployment)
1. Implement a full broker startup reconciliation of *cash, positions, all open orders*
   and fills (including orders outside the configured universe). Pending intents
   must only be cleared after positive broker confirmation.
2. Implement a real order state machine with accepted, pending cancel, partial,
   cancel, expired and rejected states; a persistent database/WAL and idempotent
   broker order identity. Test process crashes at every state transition.
3. Independently validate data session dates, exchange calendar, freshness and
   expected universe coverage before authorization. Today's sleeves alone do
   not establish a coherent, complete market-data snapshot.
4. Add an independent emergency control for cancel/flatten that works without
   fresh data, including broker position and pricing checks and escalation.
5. Test drawdown/halt on streamed equity rather than rebalance alone.
6. Add integration tests with the pinned NautilusTrader and IBKR paper gateway,
   resilience/failover tests, and operator runbooks.
7. Remove survivorship and delisting lookahead in historical backtests, replace
   Yahoo-adjusted research data with audited point-in-time corporate-action data,
   and strengthen statistical validation and strategy provenance.

**Operator warning:** Do not treat a healthy heartbeat as proof of broker
connectivity or permission to trade. The default deployment is not unattended-safe.

## Second pass: fail-closed execution and research review

- Paper execution requires a durable state path, kill switch and complete expected
  sleeve roster. The execution checkpoint is now schema version 2.
- Corrupt or legacy paper ledgers **do not auto-migrate**. First verify the actual
  account ledger, fills, cash and open orders against the broker. Preserve backups.
- Every order intent is journaled before submission. Each fill is keyed by the
  client order ID and broker trade ID; duplicate callbacks do not apply twice.
  Unknown/overfilling events latch a persistent safety halt.
- Reconciliation checks configured position quantities, out-of-universe positions,
  open orders, and in-flight orders in the reconciled Nautilus cache.
- `--adopt-broker` cannot clear an unresolved intent or safety halt. Those require
  explicit investigation and operator-controlled recovery based on broker records;
  do not casually delete the checkpoint or reset state.
- Invalid signal prices, weights, and non-finite values cannot trigger live orders.
- Critic rejection or unresolved revisions no longer fall through into validation.

**Important limitation:** the cache is not independently verified with a full
IBKR execution-history import. There is not yet a reliable cash/balance proof,
exchange-calendar scheduler, durable ACID order ledger, or an independent
broker-side kill mechanism. Do not leave the system unattended.

## Point-in-time data invariant

A stock's last available price observation is **not** a delisting announcement.
The backtester now rejects earlier-ending histories unless an independently
documented universe exit precedes the last available tradable open by enough
sessions to simulate an exit. No forward-inferred liquidation is allowed.
This does not repair unmodeled delisting returns, bankruptcy recovery,
survivorship-biased tickers, broker halted markets or corporate-action errors.
Treat missing data as an error, not a profitable trading instruction.

## Synchronized null tests

When multiple instruments share a common daily session index, the bootstrap
now uses the same resampled day-blocks for all assets, retaining common market
shocks and contemporaneous covariance. Misaligned calendars fail explicitly
rather than presenting scientifically invalid significance results. A more
general point-in-time bootstrap for staggered listings/halts is still needed;
the automated research engine must not claim significance for those panels.
