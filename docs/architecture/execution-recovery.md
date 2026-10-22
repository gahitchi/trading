# Broker execution, accounting, risk and recovery design

**Owner:** Execution/Risk Engine. **Status:** target design, NOT current behavior.
**Invariant references:** INV-01, INV-03, INV-04, INV-05, INV-06, INV-07, INV-08, INV-11, INV-14.

## 1. Single-writer and data model

Use an account-scoped transactional **SQLite WAL** store for the single-host paper prototype (fsync and durability settings documented and tested), with one writer process protected by an OS-level lease / lock. A future multi-host deployment requires a *real* distributed fencing design; never treat SQLite on a network filesystem as a safe leader-election solution.

Tables (append-only history plus materialized views):
- `deployments`: deployment UUID, account, mode (`PAPER`), broker endpoint, instrument registry fingerprint, approved strategy hashes, config hash, schema and runtime version, key creation time.
- `decisions`: sealed snapshot hash, portfolio hash, approval hash, session, risk-policy hash, equity/ledger sequence, permit expiry, status.
- `order_intents`: unique client order ID, broker account, intent hash, instrument, side, qty, type, TIF, state, reserved cash/shares and creation time.
- `order_events`: per-order append-only transitions with version, broker order ID, client order ID, timestamps and payload.
- `executions`: unique (account, broker execution ID) with cumulative fills, fees, currency and source event provenance.
- `cash_movements`: purchases/sales, commissions, dividends, fees, corrections, deposits/withdrawals, FX and corporate actions; never infer all cash movement from order fills.
- `position_movements`: instrument/lot changes and corporate actions; current position is a **materialized sum**.
- `risk_state`: persisted high-water equity and limits, sticky halts, peak source/time, daily counters.
- `reconciliation_runs`: broker API request IDs, response coverage, cash, orders, positions, executions, mismatches, proof timestamp, operator action.
- `audit_events`: operator commands, access, config migrations, alerts, severity and reason.

Every table must have appropriate primary/unique keys, foreign keys, migrations, checksums and backup verification. Never silently create an empty trading ledger when an old one or a known deployed account exists.

## 2. Submission state machine

```text
PLANNED -> RESERVED -> COMMITTED_INTENT -> SUBMITTING -> BROKER_ACCEPTED
                                                    |          |
                                                    v          v
                                                  UNKNOWN    PARTIALLY_FILLED -> FILLED
                                                    |          |                  ^
                                                    |          v                  |
                                                    +----> CANCEL_PENDING -> CANCELED
                                                    +----> REJECTED / EXPIRED / DENIED
```

`UNKNOWN` is a **blocking condition**, not an implicit rejection. A timeout may mean IBKR accepted the order; never resend it with a new ID until broker history proves the outcome. Each fill is keyed by true broker execution ID and processed once in one SQL transaction with cash, position, fees and order state. Handle late fee adjustments and order status reordering. Broker fills can arrive before acknowledgement and after a cancel request.

### Outbox pattern

1. Under transaction and single-writer lock, verify a current single-use risk permit, available reserved cash, kill status, broker account identity and duplicate intent constraints; commit order intent and reservations.
2. Dispatcher marks `SUBMITTING` durably and submits the stable broker/client ID.
3. On broker ACK, link order ID, update event log and state. On exception/timeout, mark `UNKNOWN`; **no blind retry**.
4. Inbox persists broker events verbatim and de-duplicates by *broker-native event identity*. Apply ledger effects transactionally.
5. Pending cancellation is not a terminal canceled order until the broker confirms the final status and all fills.
6. At recovery, reconcile before enabling the dispatcher; compare full account snapshot and event-history range with local ledger.

**Partial fill:** 30/100 filled, then cancel of remaining 70: position increases by 30, cash decreases by those actual 30 shares plus fees, reservation for unfilled 70 releases only on authoritative terminal confirmation. A replay of the same execution ID does not increase quantity.

## 3. Authoritative startup recovery

Always start in `DISABLED / RECOVERING` and keep broker submission code inaccessible.
1. Acquire exclusive lease; load exact account/deployment identity and ledger schema, verify storage integrity and last durable audit sequence.
2. Connect to IBKR paper gateway; positively verify configured `DU` account, currency, API connection status and account metadata. Refuse additional accounts.
3. Fetch **full** broker account cash/available funds, every open position (including out-of-universe positions), open/in-flight/completed orders, recent executions and fee adjustments for the required history window; handle pagination/reconnect and state coverage explicitly. A local cache alone is insufficient proof.
4. Match executions by immutable broker IDs. Rebuild/migrate any missing events **only through reviewed reconciliation**, never through heuristic `adopt positions at cost basis`.
5. Check broker vs internal cash, shares, reserved quantities, settlements, pending orders and risk high-water mark. Flag unrelated account activity; prevent double ownership.
6. Persist signed/hashed reconciliation report and its data coverage; require a recent complete proof and fresh broker connection.
7. Transition to `READY` only if **all** required checks pass. Otherwise `HALTED`, with alert and manual procedure.

There is no legitimate automatic reset of an unknown drawdown peak, unknown order, corporate action or missing ledger. Recovery must be safe when the process dies at *any* step.

## 4. Risk and buying-power semantics

- Risk logic needs observed broker equity, local balances and a coherent price mark with quality source. Detect gap losses, overnight movements, unrecognized corporate actions, cash shortfalls and position drift.
- Persist and recheck caps independent of strategy updates; do not reset counters on process restart. Implement per-order, per-instrument, sector, gross exposure, cash, drawdown, max orders, daily turnover, stale-price and market-halt constraints.
- Buying power reservation spans all acknowledged and uncertain orders. No new buy can depend on an unfilled sale or assumed margin.
- Fees may post after fills. Distinguish estimated, realized and corrected commissions; never discard adjustments.
- Risk permit is bound to exact portfolio, account, data snapshot, ledger version, mark price, limits and expiry; consumed exactly once.

## 5. Emergency controls

`HALT`: refuse new intents synchronously, persist reason, alert; cancel **all account-owned** outstanding orders immediately where connected, reconcile cancel outcomes. Existing fills can still post.

`FLATTEN`: separate high-risk workflow, not a synonym for kill. After halting and resolving cancellations and broker state, produce a broker-driven risk-reduction plan with fresh executable quotes and venue/order permissions. Confirm inventory and idempotent orders; account for halted/delisted markets, exchange hours and borrow/FX restrictions. If data unavailable, retain `HALTED` and page operator; never claim to have liquidated what the exchange cannot execute.

`RESUME`: requires explicit operator identity, reason, validated account/broker snapshot, healthy risk state and persisted authorization. Clearing a file does **not** reset sticky halt or authorize new trades.

## 6. Required crash/fault tests

Crash before commit; after intent commit; immediately before/after broker send; ACK never received; disconnect after ACK; partial fill then kill; fill arriving after cancel; execution replay; duplicate callback; partial database write/disk full; lost broker connection during snapshot; corporate action overnight; out-of-universe position; simultaneous second worker; 2FA/gateway restart; clock rollback.

For each: **no duplicate order, no balance corruption, a preserved durable audit trail, and no trading on uncertain state**. Chaos tests must run against a simulated broker as well as a real paper gateway at a supervised staging stage.
