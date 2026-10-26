# ADR: Independently owned trading state with external engines as adapters

**Status:** proposed architecture, first accounting-kernel slice implemented; *no*
general autonomous broker execution and *no* independent release gate cleared.

## Decision

Trading Lab will own its **causal financial and execution state machine**, its
market-data qualification, its strategy/risk authorization decisions, its durable
event journal and its broker reconciliation rules. NautilusTrader remains an
open-source, independently testable backtest/sandbox **adapter and parity
oracle**, not an unavoidable prerequisite for expressing a supported broker
corporate-action event. A full rewrite of the entire app is **not approved**:
reuse and aggressively verify components already meeting the requirements.

The initial engine-independent accounting foundation is implemented in
`src/lab/kernel/double_entry.py`. It is an exact-Decimal, standard-library-only
**double-entry projection**, *not* a new exchange matching engine, a second
simulator, an OMS or a live broker client. `CorporateSession` now persists
that projection atomically in the same SQLite transaction as each classified
corporate event. The previous financial-domain reducer and the new
double-entry projector rederive one another's effects on restart.

NautilusTrader's pinned engine remains deliberately non-authoritative for
unimplemented corporate-action transitions. The existing fail-closed delisting
policy remains intact.

## One production system, multiple independent verification paths

```text
vendor/exchange recorder + clock uncertainty + reference/master instrument data
        |
        v
causal ingestion/quarantine -> versioned events -> append-only immutable journal
        |                                                |
        v                                                v
validated observation stream                   deterministic state reducer
        |                                                |
strategy candidates -> qualification -> risk -> reservations -> supervised OMS
                                                       |      |         |
                                                       |      |         +--> authenticated paper adapter
                                                       |      +--> reconnect/open-order reconciliation
                                                       +--> explicit order/cancel outbox
                                                                  |
                                                       journaled broker receipts/fills
                                                                  |
                                          exact double-entry ledger + state replay
                                                                  |
                                         broker/native source-of-truth parity
```

The **core contracts** are versioned, venue-independent and fully auditable:
- `InstrumentDefinition` (decimal price/quantity step, contract multiplier,
  corporate-action version, currency precision, venue capabilities).
- `Observation` (venue event time, recorder receipt time, decision availability,
  source identity and hash, sequence certainty and uncertainty policy).
- `OrderIntent` / `Reservation` / `OrderLifecycle` (account, unique scoped
  ID, explicit quantities, remaining open orders, cancel-requested vs
  confirmed cancelled, fill IDs and source acknowledgements).
- `AccountingEvent` and balanced `PostingBatch` (immutable business event
  and double-entry cash/basis/claim/earnings effect), with proven event time
  and availability time and a crash-replayable state digest.
- `ReconciliationEvidence` (real authenticated broker positions, open orders,
  cash and corporate actions and version/time boundaries). Caller-supplied
  `verified=True` flags never prove reconciliation.

**Exactly-once matching across networks is not assumed.** A client timeout,
ambiguous broker ack, or cancel request freezes resubmission pending a broker
order-ID/history reconciliation. Same-side pending exits reserve reduction
capacity, including unsettled cancel requests and partial fills. Rounding
toward zero for lot-step is mandatory; an ex-date split invalidates all
position/order/lot-step assumptions until broker reconciliation finishes.

## Kernel invariants

For a single verified settlement currency, signed debits are positive:

| Account | Long book sign | Short book sign | Economic meaning |
| --- | --- | --- | --- |
| `cash` | + | + | Actual deposited/paid balance |
| `security_basis` | + | − | Historical cost or short sale liability |
| `corporate_claim` | + | − | Receivable / payable, *not spendable cash* |
| `realized_earnings` | − for profits | + for losses | Income statement credit balance |

Conservation: every batch's debits plus credits equal exactly **zero**,
including zero-valued write-offs. The opening capital balance is unchanged
through an internal corporate action. A share retirement never credits cash
before an independently sourced receipt. A corporate cash payment can move
only actual cash and its offsetting payable/receivable; it cannot silently
change basis or P&L. Replay recomputes the entire expected posting vector,
its account order, precise string values and financial closing-state hash,
not merely whether the submitted numbers happen to sum to zero.

The scoped journal moves from corporate schema v1 to **v2**. A v1 journal is
**not silently upgraded**: an offline audited migration must replay source
events into a new journal and demonstrate the independent double-entry
projection. Unrecognized old journals fail closed. A cryptographic hash chain
is local integrity detection, not a signature protecting against a
filesystem-privileged adversary.

## Staged engineering and architecture selection

**A — Current release candidate:** preserve pinned Nautilus strategy/backtest
data path and existing paper-only policy; add dual-run verification using
the independently owned kernel. F1 corporate action stays disabled where
native settlement cannot be expressed faithfully. All existing seven release
criteria remain blocking.

**B — Broker-neutral paper service:** after explicit review, implement one
single-writer, append-first OMS (order ID evidence, broker reconnect, stale
clock quarantine, reduce-only safe sizing, failure/recovery) on the new core.
The same journaled domain events and accounting must run with two adapters:
offline deterministic harness and supervised authenticated *paper* broker.
Nautilus simulation is an independent **comparison path**, not a bookkeeping
source of truth. Require equity/cash/corporate action parity under injected
disconnect, duplicate, partial fill, margin and stale-data faults.

**C — Independent engine:** only if A/B demonstrate real capabilities missing
from pinned NautilusTrader, progressively replace that single unsupported
component with our audited source-owned implementation, preserving the same
event contract and adversarial tests. Avoid a big-bang replacement or a cheap,
unvalidated matching-engine approximation. The research harness can continue
to run Nautilus and a second independent fill oracle as differential tests.
Do not claim that a cost-basis ledger is a complete multi-asset position/risk
engine: futures variation margin, FX cash legs, securities stock-loan/borrow,
tax/withholding and derivatives funding require distinct proven reducers.

**D — Deployable application:** UI and one-click controls are *operators*
above the qualified engine, not alternate execution authorities. No action
may bypass source, financial and safety gates. A dedicated supported host
is needed for reliable 24/7 operation; Termux can execute portable financial
tests and offline scenarios but is not a qualified broker host.

## Engineering gates and external dependencies

- F0: immutable candidate tag, artifact digests, CI and independent review.
- F1: independent ledger and double-entry posting evidence are foundational
  **partial** progress; real/native/broker corporate-action parity remains
  **BLOCKED**. The previous failure to settle with genuine events must never
  be masked with fabricated OHLCV.
- F2: measured clock/venue/recorder/broker availability guard and
  source-qualified observation ingestion.
- F3/F4: real broker order lifecycle, reservations, margin/liquidity and
  cancel/late-fill reconciliation; no silent assumptions about native
  REDUCE_ONLY support on equities.
- F5–F7: reproducible strategy and governance research, supervised paper
  deployment, adversarial recovery and controls.
- F8: independent security/operational review; live capital remains **disabled**
  and must not be turned on by choosing the independent backend.

**Next executable slice:** prove the v2 corporate accounting journal with
Android/Termux standard library + both pinned CI Python versions; then
derive broker-neutral order lifecycle events and an explicit transition table.
Do not wire cash settlement into real execution before authenticated broker
reconciliation and independent economic-source evidence are supported.
