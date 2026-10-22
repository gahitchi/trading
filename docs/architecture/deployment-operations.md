# Deployment, security, scheduling, observability and operator procedures

**Owner:** Site Reliability / Operations. **Status:** target design.
**Invariants:** INV-01, INV-06, INV-07, INV-08, INV-11, INV-12, INV-13, INV-14.

## 1. Deployment topography

One dedicated paper-execution host/process per broker-account namespace, plus separately isolated research workers. The execution host mounts read-only approved strategy artifacts and configuration, and a durable storage directory. It cannot load arbitrary research checkout files.

Example scope:

```text
[Read-only research artifact registry]
                |
      [Paper decision runtime]---[Durable WAL + audit + backups]
                |                      |
         [Risk + outbox]          [Health exporter]
                |
    [Locked-down IBKR paper gateway]
                |
         IBKR DU account only
```

No automatic upgrade to live-money account is supported. Fail on unknown broker endpoint/port or any account not matching the exact configured DU identity. The gateway being reachable is not proof of login, account validity or complete order state.

## 2. Runtime lifecycle

`BOOT → LOCK_ACQUIRED → STORAGE_CHECK → BROKER_AUTHENTICATED → BROKER_RECONCILED → READY → SESSION_COLLECTING → ORDERING`.

Separate states `DEGRADED` (research/market data issue, no new orders), `HALTED` (risk/kill/manual halt), `RECOVERING` (broker/orders uncertain), `STOPPED`. No transition directly from `BOOT` or `RECOVERING` to `ORDERING`.

Mandatory startup gates:
- approval manifest/config fingerprint matches environment, account and instrument registry;
- database schema and integrity check pass, exclusive writer lease held;
- broker actual account identity confirmed; connection and authenticated state recent;
- all orders, cash and positions reconciled, including unmanaged/external activity;
- current risk state and sticky halt checked, readiness depends on reason for any freeze;
- exchange session/time sync known;
- persistent storage and alert routing tested.

If a gate is unavailable, service may run in a **read-only/health** mode; do not allow order submissions.

## 3. Hardening of the host and containers

- Pin Python toolchain, *resolved* package hashes (`uv.lock`) and container image digests (including IB gateway). Separate runtime and developer/test dependencies.
- Run as non-root, no privileged Docker, no host PID/socket mount, read-only root filesystem when possible, drop Linux capabilities, restrictive seccomp and AppArmor/SELinux, limited memory/CPU/PIDs/file descriptors, and health checks with a fixed liveness threshold.
- Keep secrets out of source, logs, prompts and artifacts. Supply via protected secret mounts or secret manager; least-privilege access, rotation/expiry, audit.
- Restrict network paths: broker adapter can reach only gateway, gateway only required IBKR endpoints, research containers **cannot access gateway**. VNC must be disabled or strongly authenticated and available only via authorized local tunnel.
- Synchronize host UTC and NYSE calendar; fail on severe clock skew. Store all instants in UTC and journal the exchange session ID.
- Use a *single writer* process lock. Docker `restart: unless-stopped` and `depends_on` are convenience, not a concurrency or readiness protocol.
- Persistence: protected directory, disk-space alarms, backups with integrity verification and restore drills. Never mount a volatile temp directory for the execution ledger.
- Validate deployment configuration via schema: limits, broker/account, mode, strategies, instrument IDs and permissions. Warn or reject unusual but legal values.
- Pin promoted artifacts by digest and verify release provenance (CI identity, reviewer and manifest signature).
- Enforce immutable audit retention policy; redact personally identifiable/account data in exported logs; document access and retention.

## 4. Observability model

Separate four independent health signals:
- **Liveness**: process exists and event loop is running;
- **Broker connectivity**: authenticated expected paper account, request/response liveness and complete reconciliation snapshot;
- **Data readiness**: correct exchange session, expected instruments/sleeves, finalized bars, snapshot sealed, no invalid updates;
- **Trading authorization**: no sticky halt, valid risk policy/permit, exclusive lease, settled/available cash, no unknown intents and no kill command.

`healthy` must never conflate these. The runtime exports:
`broker_connected`, `broker_account_verified`, `reconciliation_age_seconds`, `market_data_last_complete_session`, `decision_status`, `risk_halted`, `unknown_order_count`, `inflight_order_count`, `cash_delta`, `position_delta`, `wal_fsync_latency`, `disk_free_bytes`, `notification_delivery_status`, `clock_skew_seconds` and `strategy_artifact_hash`.

Alert with explicit severity and routing on: wrong broker account (critical), unknown order/fill (critical), ledger corruption (critical), missing market snapshot (major), risk drawdown (critical), broker disconnect (major), scheduled no-trade due to holiday (informational), stale reporting/notification failure (major). Alert channels must be tested with an outage fallback.

## 5. Scheduling and failover

Exchange-calendar source determines valid sessions and half-days; scheduler runs in the correct NY timezone and must handle DST differences between EU/US. Every scheduled job has an idempotent session key. Never silently make up a missed order after cutoff; missed decisions require operator-approved recovery based on time/venue/market constraints.

At IB Gateway restart or session expiry: immediately disable dispatcher, keep recording any late broker events, restart broker reconciliation, then resume only after all gates pass. If the host is unavailable, **no automatic split-brain failover** without fencing and verified broker order history.

## 6. Release / rollback

- Build: reproduce from locked dependency hashes, static checks, full unit/property/chaos suite, image signing/scanning, data/model provenance tests.
- Shadow mode: run the full decision pipeline without order calls; compare every snapshot and proposed order to known test fixtures.
- Supervised paper: order path end-to-end, partial fills, cancel retries, gateway restarts, holidays and settlement cases validated.
- Unattended paper can be considered only after sustained fault-free shadow/supervised runs and formal acceptance evidence. Do not infer readiness from a green CI badge.
- Deploy only to an account allowlist, immutable artifact and reviewed config. Rollback freezes order dispatch first; **retains the same durable ledger and broker order namespace**, reconciles and then selectively resumes.
- Run quarterly recovery drills and after any ledger/order-schema migration; verify backups by restoring to a separate machine *with broker sending disabled*.

## 7. Operator workflows

Every operator action includes actor identity, UTC timestamp, reason, affected account/deployment, precondition checks, target state, confirmation and audit event.

`halt`: persist first, block submission immediately, request verified cancellation, alert.
`flatten`: separate supervised broker-position procedure; not guaranteed during halts/no market.
`resume`: no resumption if an order is UNKNOWN, risk halt unresolved, broker missing, wrong account or a ledger mismatch.
`release`: freeze snapshot/config/strategy hashes and pass dry-run; activate only at an exchange-session boundary.
`restore`: isolate from broker, restore database and backups, replay events to consistency, reconnect **in RECOVERING**, and compare with broker.
`change universe`: requires migration and reconciliation; cannot silently discard out-of-universe positions or stale open orders.
