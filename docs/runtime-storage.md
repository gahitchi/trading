# Durable runtime and upgrades

The authoritative execution state is now `results/paper/ledger.sqlite3`. One FULL
SQLite transaction commits cash, positions, daily caps, pending intents, terminal
attribution and applied trade IDs. `ledger.json` is a small diagnostic projection
and may lag after a crash. **Do not edit it to change the book.** The schema-3 projection deliberately makes older binaries refuse the ledger;
downgrading to a JSON-only binary is unsupported. Existing v1/v2 JSON
ledgers migrate transactionally on first opening. Missing/corrupt stores fail closed.

Terminal orders and trade IDs have indexed disk storage and are never expired on an
assumed broker replay horizon. Only active intents and unsaved history changes live
in Python memory. The default database capacity is 2 GiB; exhaustion stops execution
rather than forgetting a trade. Plan storage and inspect usage during the broker
soak. Nautilus' own cache and broker history retention still need deployment soak
qualification. There is no claim that every process resource is now bounded.

Notification storage admits at most 10,000 queued messages. An unavailable/full
outbox or a delivery backlog older than five minutes blocks new exposure when
notifications are enabled. Reductions remain possible. Delivery is at least once;
duplicates after uncertain delivery remain possible. A failed enqueue is logged and
must be investigated. Existing queued messages are not discarded to make room.

## Cold backup and code upgrade

```sh
# Stop the paper node first. Commands refuse an active runtime writer.
lab runtime backup --destination /private/backups/paper-before-upgrade
lab runtime preflight
```

Backups include the entire runtime, permanent deduplication history, control state
and key, and a hash manifest. They are private; never commit or share raw backups.
Take a filesystem/storage snapshot as an additional deployment-level protection.
Restoring old ledger state after later fills is unsafe; broker reconciliation is
required before a restored runtime can trade.

A code-only identity upgrade is implemented for a **flat book with no unresolved
intents**, using a fresh operator broker attestation. The attestation is external
input, not something this tool can fabricate or independently verify. It must name
the exact old runtime identity and ledger hashes reported by preflight, the configured
paper account, and actual broker state with no positions or open orders:

```json
{
  "captured_at": "2026-10-23T12:00:00+00:00",
  "runtime_identity": "old identity SHA256 from preflight",
  "ledger": "ledger SHA256 from preflight",
  "account": "configured paper account",
  "open_orders": [],
  "positions": {}
}
```

```sh
lab runtime upgrade --evidence /private/broker-attestation.json \
  --reason 'broker history and flat book reconciled; candidate independently reviewed' \
  --destination /private/backups/paper-upgrade
```

The attestation must be at most ten minutes old. The upgrade makes a cold backup
and audit receipt before updating only implementation identity. It does not change
cash, trade IDs, strategy approvals, account, configuration, hard halts or positions.
The old authority mandate no longer matches; explicitly issue a new one when starting
the reviewed candidate. Research artifacts need revalidation against changed source.
Legacy identities that did not bind execution settings require an independent migration
plan. Non-flat migrations, account changes, strategy/configuration migrations and recovery
from ambiguous broker state still require an independently reviewed migration plan.
