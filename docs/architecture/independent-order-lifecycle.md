# Independent order lifecycle — F3/F4 offline reference, not broker execution

**Status:** prototype **offline-only**, independently owned, stdlib-only lifecycle and durable replay. Does **not** close F3 or F4 until authenticated broker adapter, actual reconciliation and execution authority are separately qualified.

## Durable state and conservative release

`src/lab/kernel/order_lifecycle.py` reuses the existing `EventJournal` rather than building a second OMS database. Its `OfflineOrderSession` is scoped to one instrument and a pinned action version and **an operator-asserted initial position**. All state transitions append an exact event, closing digest and source metadata before in-memory acknowledgement. A restart rebuilds states and projection from that same journal and checks every resulting state digest. No network, account credentials, broker API, Nautilus dependency or actual order sending exists.

| Transition | Preconditions | Consequence |
| --- | --- | --- |
| `intent` | typed **reduce-only** side, positive step-aligned Decimal quantity, held inventory less all existing reservations, matching instrument/action version | reserves full quantity, **no submission** |
| `submitted` | exactly one offline dispatch preparation, no unresolved source freeze | durable preparation only; **no network** |
| `ack` | original submission exists, unique broker-order ID, source-identified event | records reported external identity, not auth |
| `fill` | submitted; unique global execution ID; exact positive step-aligned qty <= remaining | advances local projected position, leaves remaining quantity reserved |
| `cancel requested` | unresolved submitted order | **does not** release outstanding reservation |
| `cancel confirmed` | matching pending cancellation | marks cancelled but **keeps remaining reserved and freezes all new intents/preparation**; broker order history must independently exclude late fills before future release |
| late fill after cancelled | previously submitted, qty valid, unique exec ID | records fill and **stays frozen**, recalculates remaining reserved qty |
| `unknown` | an unresolved dispatch/ack has uncertain broker status | freezes new intents and new preparations; no automatic retry |
| rejected | full unfilled rejection only | terminal in offline projection; production must prove broker/venue evidence before release |

Idempotency is scoped to a stable source event ID plus exact payload and availability time. The same event can be reprocessed only without any second accounting effect. Conflicting reuse, duplicate executions across orders, non-monotone observation availability, unrecognized journal events, forged source IDs/digests, foreign instrument/split version or replay digest divergence fail closed.

The **starting position is NOT a broker readback** and `BrokerEvidence` source fields are **metadata, not cryptographic authentication**. Internal fills adjust only `position_from_fills`, a clearly unauthenticated projection; it is not a brokerage position and never updates cash, margin or the independent double-entry kernel yet. The session is deliberately one instrument: there is no multi-asset portfolio-capital planner, venue-level risk approval or cross-account lock.

The reference does not automatically unfreeze a canceled/unknown order. A later *separately audited* broker-history, order/position/lot-step, split-version, cash and margin reconciliation protocol must be added before reservations can be released in production.

## Independent implementation path

1. Keep this canonical event schema/transition table and run the portable suite across Android and desktop Python without broker credentials.
2. Implement a broker adapter that provides authenticated, sequence-consistent position/open-order/execution evidence and scoped client IDs. Any ambiguous placement, cancel or reconnect remains quarantined.
3. Join the order execution lifecycle to the independently owned financial kernel in a single authoritative journal and validate all actual cash/fees/realized and unrealized PnL.
4. Add multi-instrument risk reservations and margin/FX/borrow rules without inventing broker mechanics or relaxing research qualification.
5. Differential-test against NautilusTrader *as one of several independent oracles*; qualify supervised external **paper** operation and operator controls separately. No live-money path is enabled.

## Test acceptance

Native Termux or hosted Python can run without scientific or Nautilus packages:

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/independent-order-lifecycle-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

`tests/test_portable_order_lifecycle.py` covers retry prevention, partial fills, conflicting source events, unknown placement state, cancellation races, late fills, idempotent recovery, global execution identity, arbitrary Decimal lot steps, forged but hash-valid journals and no-third-party imports.

**Release status:** F1 accounting foundation is partial. F3/F4 independent OMS reference is partial. Broker-native parity, authenticated reconciliation, capital risk, F5 strategy qualification, F6 supervised paper trading and F8 release/security signoff remain separately blocked.
