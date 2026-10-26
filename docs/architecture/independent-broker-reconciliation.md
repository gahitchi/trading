# Read-only broker reconciliation: independent reported-state comparison

**Status:** portable **offline evidence-consistency oracle**, not an authenticated
broker reconciler, executable OMS, account-lease, or release of reservations.

## Current executable implementation

The new `lab.kernel.broker_reconciliation` consumes a copied
`OfflineOrderSession` and a typed, immutable *reported* `ReportedSnapshot`
with account scope, instrument, corporate-action version, as-of/availability
timestamps, local-source fingerprint, positions, order-history entries and
execution-history entries. The snapshot's completeness/fence booleans are
**assertions by its source, not verified truths**.

`compare_reported_snapshot()` inspects the journal integrity and exact
committed head, and compares:
- snapshot visibility/availability boundary versus the last local event;
- instrument and action-version scope, reported position against independent
  projected fills, and declared positions/orders/executions/causal coverage;
- each prepared client-order ID and known broker-order ID, cumulative filled
  quantity, terminal status, open/cancel-pending status and unknown states;
- all locally journaled execution IDs, quantities and owning order versus
  reported executions, plus any unjournaled extra orders/executions;
- outstanding locally quarantined states or source-unknown/cancel-race states.

It returns sorted mismatch codes and
`ParityAssessment.internally_consistent`. That property means only that
*reported bytes* agree with the local journal under the stated conditions.
It **never means** authenticated broker custody, causal completeness, or that
the broker will not subsequently deliver a late fill.

The deliberately unchangeable `ParityAssessment` fields
`source_authenticated=False`, `broker_authorization=False`, and
`release_reservations=False` are not a configurable shortcut. **No function
mutates OMS state, releases inventory or routes an order.** The source hash
identifies a claimed export, not a cryptographic brokerage signature.

## Provenance and broker protocol still missing

The next independent production implementation must establish a durable
**account-scoped broker session** with authenticated identity, account/venue
namespace, complete cursor-fenced open **and closed** order history, execution
replay across reconnect, clock uncertainty bounds, source sequence continuity,
split-adjusted quantity/price definitions and a traceable custody statement.
It must reconcile all orders including those never observed by the local
process, all fills/fees/cash and margin, and any corporate actions. An
unconfirmed cancel, timeout, pending GTC modification, duplicate client ID
across accounts or unknown open order must suspend new placement and freeze
previous capacity. Source coverage flags alone cannot authorize release.

Once that is independently audited, a **separate, explicitly versioned,
append-only recovery transition** could issue a durable reconciliation result
as part of a single-writer OMS protocol. That transition should require all
source identity/cursor evidence, a corresponding journal and accountant
projection, a complete order/fill after-cancel fence and an authoritative
broker position. Any error must remain frozen.

**This stage intentionally does not implement that transition**, because no
authenticated live/paper broker adapter or corresponding verified evidence
exists yet. Treating this reported-data comparison as a broker authorization
would be a blocking security defect.

## Tests and gates

`tests/test_portable_broker_reconciliation.py` covers matching reported
positions/orders/fills, unknown/out-of-band fills, missing prepared orders,
duplicate IDs, stale data and split versions, wrong order ownership, status
races, incomplete declared coverage, immutable strict quantities and
stdlib-only import on native Termux. Tests are synthetic, with no credentials
or broker endpoints.

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/broker-reconciliation-oracle-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

F1, F3/F4, F6 and F8 remain **blocked**. This component introduces no
authorization policy, so a local `internally_consistent=True` is not evidence
that any release criterion has been achieved.
