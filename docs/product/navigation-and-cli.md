# Navigation, API and CLI interaction specification

**Status: planned command/API grammar only.** The current repository has a `lab` CLI with `data`, `research`, `paper`, `health`, `kill`, etc. Commands below do **not** exist unless explicitly marked current. Never paste an example without checking which version is deployed.

**Implemented (research-only) addition:** `lab analyze --tickers ... --as-of ... --json`
produces exploratory historical market/risk reports with an immutable input
fingerprint; unlike most planned commands below, `lab analyze` is available
on the hardening branch. It does not deploy a strategy or approve an order.

**Implemented on the hardening branch:** `lab forecast --ticker GOOG --as-of YYYY-MM-DD --json`
provides an exploratory causal model, empirical interval, risk-regime
probabilities, feature-drift abstention and a frozen OOS/baseline comparison.
It is not a live forecast service and cannot submit orders.

**Implemented offline research addition:** `lab capital-audit --manifest portfolio.json --json`
checks a multi-asset cash/FX/share/bond portfolio and separately reports
explicit scenario shocks, factor attribution and hypothetical risk policy
breaches. It cannot execute Forex or security orders and it must not be
interpreted as a deployment readiness indication. See
[multiasset-reasoning.md](../architecture/multiasset-reasoning.md).

## 1. Progressive disclosure

The entry screen must separate:
1. **What is happening?** Mode, session progress, last broker reconciliation, data completeness, health, order uncertainty, alert queue.
2. **What can I do safely?** Investigate, export report, change bounded research plan, request supervised action.
3. **What must I not do?** Trade with missing final bar, bypass incomplete broker history, reset risk high-water mark, equate "acknowledged" with recovered.

The first screen should be readable in under 10 seconds and answer the top question without nested CLI calls. Complex numeric data and forensic event traces are one drilldown away.

## 2. Proposed CLI language

| Intent | Proposed command | Result/behavior |
| --- | --- | --- |
| Overview | `lab status` | Deterministic read-only status (or UNKNOWN when proof absent) |
| Readiness report | `lab readiness --format json` | Separate liveness, broker, data, risk, execution and authorization gates |
| Configure | `lab setup` | Guided wizard with redaction and dry-run before writing |
| New research project | `lab project create --manifest research.yaml` | Validated, immutable project ID; no broker access |
| List research jobs | `lab project jobs <id>` | State, cost, hypothesis count, deterministic run IDs |
| Get full hypothesis evidence | `lab strategy inspect <hash>` | Dossier and sealed code/data hashes |
| Validate data | `lab data audit --dataset <id>` | Coverage, quality flags, missing/excluded names, vendor hash |
| Start reproducible evaluation | `lab evaluate <hypothesis> --manifest experiment.yaml` | No holdout unless separate permit |
| Seal holdout access | `lab holdout request <candidate>` | Explicit scoped authorization; records trial and reason |
| Approve shadow | `lab strategy approve <hash> --scope shadow` | Signature + bounded expiry, never deploys alone |
| Preview portfolio | `lab portfolio preview --session <id>` | Read-only targets, risk, fee and cash budget |
| Start shadow | `lab deploy shadow --release <hash>` | Broker order channel physically disconnected |
| Request supervised paper | `lab deploy paper --release <hash> --dry-run` | Preflight proof; creates approval request |
| View account reconciliation | `lab broker reconcile --read-only` | Evidence and differences; does **not** mutate cash ledger |
| Inspect incident | `lab incident show <id>` | Causal chain, missing evidence, safe next step |
| Halt | `lab operations halt --reason "..."` | Durable immediate deny-new-orders; audited |
| Request flatten | `lab operations flatten --reason "..."` | Escalated, supervised risk-reduction workflow |
| Resume | `lab operations resume --approval <id>` | Requires all gates and scoped operator grant |
| Audit trail | `lab audit export --from ... --to ...` | Redacted default and replayable schema |
| Verify backup | `lab backup verify <backup-id>` | Isolated, offline read; cannot submit orders |

### CLI conventions

- Mutations require a **preview** with exact consequences and `--confirm <scoped-action-id>` or an equivalent authenticated confirmation; `--yes` is not allowed for risky broker or approval actions.
- `--format json` returns a versioned structured schema with stable status enums and ISO 8601 timestamps; no parsing human prose in automation.
- Exit codes: `0` success, `1` known negative result/not ready, `2` invalid arguments/config, `3` broker/data unavailable, `4` denied by authorization/risk policy. These are proposed and must be documented/tested.
- `--dry-run` never mutates state or contacts an order-accepting endpoint.
- Reason codes and incident IDs are stable and link to a human-readable problem description.
- Every command prints the **operating mode** and whether it can create, cancel or modify orders. No hidden trading side effects.
- Never log access tokens, IB passwords or full broker-account identifiers in exported diagnostics.
- Command help must distinguish **existing** commands from the future target grammar. Deprecate unsafe legacy commands only after a migration and operator runbook.

## 3. Read-model schema (proposal)

```json
{
  "schema_version": 1,
  "workspace_id": "ws-001",
  "mode": "SHADOW",
  "health": {
    "liveness": "READY",
    "broker": "UNKNOWN",
    "market_data": "DEGRADED",
    "trade_authorization": "DISABLED"
  },
  "session": {
    "exchange": "XNYS",
    "day": "2026-10-22",
    "state": "COLLECTING",
    "snapshot_hash": null
  },
  "attention": [
    {
      "incident_id": "INC-001",
      "severity": "MAJOR",
      "reason_code": "MISSING_FINAL_BAR",
      "affected_instruments": ["SPY.ARCX"],
      "requires_operator": false
    }
  ],
  "last_reconciliation": null,
  "risk": {
    "sticky_halt": false,
    "unknown_orders": null
  },
  "generated_at_utc": "2026-10-22T20:15:00Z"
}
```

An absent fact is `null` or `UNKNOWN`, **not** false or zero. The UI converts timestamps according to user preference, but the event store remains UTC and the trading session is always exchange-local.

## 4. API boundaries

- **Query API** serves read-only snapshots, experiments, strategy dossiers, incidents, sessions, broker reconciliation reports and audit exports.
- **Command API** accepts authenticated, narrowly scoped operator actions; validates the current state machine and writes audit events. It cannot bypass the execution engine or send raw broker commands from the browser.
- **Event subscriptions** push state changes to authenticated local UI; messages include event IDs and durable sequence, allow reconnect, and detect gaps.
- **Alert gateway** sends redacted summaries over optionally connected channels; link tokens are short-lived; no secret is embedded in alerts.
- **No remote public API** until an independently reviewed auth, CSRF, network and secret-storage threat model exists.

## 5. Dashboard and accessibility requirements

- An always-visible **Operating Mode** indicator and **Order Submission State** indicator, not just "online".
- Every status has meaning (READY, WAITING, DEGRADED, UNKNOWN, HALTED), timestamp and source.
- Watchlists and figures are subordinate to outstanding safety incidents.
- Show why an order was *not* placed, alongside why it was placed.
- Keep state changes auditable. Time-consuming jobs show exact stage, last completed artifact, elapsed time and stoppable boundaries.
- On mobile, default to reading statuses and acknowledging information. No unintended order path through push-notification links.

## 6. Migration from present-day CLI

Keep the current `lab data`, `lab research`, `lab health` and `lab paper --smoke` for development. Design the new read model, statuses and versioned event protocol first. Do **not** rebrand existing `lab resume` or `lab paper --adopt-broker` as safe recovery: their current semantics require separate safety redesign. Feature-gate or retire risky commands only when operator migration is explicit and tested.

**Sequence:** immutable snapshot read model → CLI status/report → local web UI → authenticated operator commands → qualified paper deployment. Don't build a glossy dashboard on top of unreliable or ambiguous operational state.
