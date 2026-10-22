# Architecture decision register

An ADR is an auditable choice, not an implementation claim. All entries are **PROPOSED** until reviewed; update to Accepted / Superseded with code commit and test evidence before treating them as policy.

| ADR | Decision | Rationale | Rejected alternative / caveat |
| --- | --- | --- | --- |
| ADR-001 | Strict paper-only system; separate live-money product never enabled by flags | Constrains worst-case harm and verification scope | A mode toggle risks accidental real trading |
| ADR-002 | Isolate research from broker-connected runtime | LLM code and dynamic imports are untrusted | AST filtering alone is not OS isolation |
| ADR-003 | Session-snapshot builder precedes portfolio/risk/execution | Solves partial and mixed-date decisions by construction | One boolean `new_data` flag or allocator-only completeness gating insufficient |
| ADR-004 | Explicit, single-use risk permit tied to immutable decision hash and ledger sequence | Eliminates asynchronous approval race and stale risk | Publish/subscribe ordering is not proof of authorization |
| ADR-005 | Single-host SQLite WAL transactional outbox/inbox as initial source-of-truth journal | ACID persistence and crash-point auditability | Ad-hoc JSON checkpoint cannot atomically record intents/fills/cash; multi-host requires different locking |
| ADR-006 | Broker account observations + app intent journal are independently authoritative and must reconcile | Detects unknown orders, external activity, fee/corporate-action corrections | Silently replacing local positions from cache hides discrepancies |
| ADR-007 | Any uncertain submit/cancel remains `UNKNOWN` and blocks new trades | Prevents accidental duplicate orders | Blind retry on timeout unsafe |
| ADR-008 | Canonical instrument ID, venue and currency, not mutable ticker, are accounting keys | Survives ticker reuse, splits and migrations | Trading symbol alone ambiguous |
| ADR-009 | Prices as decimal/fixed-point per instrument tick/lot; instants in UTC; session as exchange calendar key | Avoids floating point bookkeeping drift and time-zone ambiguities | Binary floats for broker money not acceptable for authoritative accounting |
| ADR-010 | Separate halt and supervised flatten state machines | Flatten can fail on halted markets or missing quotes | `--flatten` flag should not imply guaranteed liquidation |
| ADR-011 | Sealed experiment manifest and complete trial log | Mitigates overfitting, retroactive holdout selection and invisible data changes | A strategy-name-only fingerprint inadequate |
| ADR-012 | Immutable promoted strategy digest verified at load, isolated generation and human approval | Detects post-test code tampering | Mutable `status.json` + dynamic import unsafe |
| ADR-013 | Liveness, connectivity, data readiness and trade authorization are distinct metrics | A recent heartbeat cannot verify broker connection | Docker process health alone insufficient |
| ADR-014 | No auto-recovery from missing/corrupt persistence when prior deployment is known | Missing ledger/peak destroys ability to reconcile safely | Treating missing file as new account risks double exposure |
| ADR-015 | No automatic split-brain failover without fencing | Single account/order namespace has one writer | `restart: always` on two nodes could duplicate orders |

## Open questions requiring implementation research

1. Which exact NautilusTrader 2.0.0rc7 APIs provide reliable broker account balances, all open and completed orders, order IDs and execution-history coverage? Test against pinned adapter; never guess.
2. Which IBKR order types/TIFs and scheduled submit times reliably model exchange opening orders for each managed venue? Validate permissible market-on-open/auction behavior.
3. Does the account expose settlement, cash movements, fee adjustments and corporate actions with sufficient history through API, or is a second authoritative statement feed necessary?
4. Which exchange calendar package/version and which bar provider contract identify finalized session data and early close?
5. What position/price/cash reconciliation tolerances are permitted by broker data rounding and which must be exact?
6. How is migration from current `ledger.json` to SQL proven correct and operator-approved without losing prior orders?
7. How are strategy artifact signing, trust roots, rollback and SBOM pinned in the CI/build infrastructure?
8. What operator SLA, maximum no-data delay and alerting escalation are required before a real unattended soak test?
9. How does the full system behave if a manual broker order or deposit/withdrawal occurs on the same DU account?
10. What is the acceptable frozen/rejected-trade behavior for planned universe changes or delisted instruments?

**Decision rule:** a design uncertainty affecting loss, duplication, or account identification defaults to `HALTED` until experimentally resolved.
