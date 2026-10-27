# Private IBKR paper capture -> independent OMS reconciliation (read-only)

**Stage:** integration bridge, experimental. **No authenticated custody, complete order-history coverage,
new order submission, release of reservations, or automatic recovery authorization.**

## What is implemented

`lab.capture.snapshot` runs an existing NautilusTrader `DataActor`, not an execution strategy:
it cannot place or cancel orders. The snapshot output now includes
`broker_capture_meta.json` with **local** capture-start/completion nanoseconds and three
explicitly **false** verification flags. `broker_positions.json` includes a new
`net_exact` string reconstructed from native `Quantity` units and position
side, alongside its older informational floating-point `net` field. Old
captures lacking exact quantities or the metadata file fail closed.

`lab.kernel.paper_capture_bridge.load_paper_capture(...)` accepts an
already saved **private** raw capture directory and an independently pinned
paper-account scope, ticker, instrument ID and instrument action version.
It refuses unsupported paper ports or account mismatches, missing or
symlinked evidence files, malformed JSON, stale/inconsistent local capture
times, non-exact position components, missing broker IDs and partial
order/fill histories. It binds six file hashes into a deterministic local
SHA-256 manifest and projects orders and fills to
`lab.kernel.broker_reconciliation.ReportedSnapshot`.

This is a pure standard-library transformation: the bridge never imports the
Nautilus runtime or establishes a broker connection. It exposes a `compare`
method which calls the existing read-only journal-versus-reported-state
oracle; **all four reported coverage flags remain false**, regardless of
source input. Even matching position, orders and execution quantities result
in explicit coverage findings. `can_submit_orders`,
`broker_authenticated` and `can_release_reservations` always return
`False`. It cannot qualify a broker adapter, give a green trading signal,
issue an authority token, or modify the journal.

A broker-facing copy may have been generated while IB Gateway was logged in,
but a locally written hash and the account ID printed inside a JSON object
do **not** authenticate the broker. Local `capture_completed_ns` is the
end of the observation interval, **not** a synchronized broker as-of cursor.

## Local inspection command

After a read-only capture is created by the existing controlled snapshot workflow, a
standard-library-only diagnostic can inspect its **private raw** files. It takes the
account and port from the operator's local `lab.toml`, **not command-line credentials**:

```sh
PYTHONPATH=src python -m lab.kernel.paper_capture_bridge \\
  --config /path/to/lab.toml \\
  --capture /private/outside-git/B03-daily/snapshot-directory \\
  --ticker GOOG --action-version v1
```

Paper settings follow the exact same operator environment precedence as
the capture command: `LAB_PAPER_ACCOUNT` overrides `[paper] account_id` in
`lab.toml`, and `LAB_IB_PORT` overrides the configured paper API port.
An IBKR *username* is not the `DU...` or `DUR...` paper-account ID.
For a temporary terminal-only acceptance session, prefer the environment
override rather than committing an account identifier into the repository.
A missing or invalid paper setting is reported without echoing account IDs.

This command makes no network connection and writes nothing. The output contains
only a capture digest, snapshot token, event counts and explicit
`UNQUALIFIED_PRIVATE_CAPTURE`/`UNVERIFIED` statuses, never raw account or
trade IDs. Exit code 0 means that *this private file set could be parsed*; it
does **not** approve a strategy or attest that IBKR emitted complete data.

For the candidate B01 read-only checklist, append `--b01-report`. It inspects
the bounded, exact-Decimal broker-account balance rows, local read-only
observation interval and capture freshness after independently re-hashing the
files. The structured output is always `"qualification": "BLOCKED"` with
the outstanding broker-identity, history, cash/fees, clock and data-permission
requirements listed, even when the observed account number matches the paper
configuration. The capture clock now begins when the DataActor requests the
broker observations, not when the final dump starts. It does not prove the
broker's as-of time or completeness.

**Operator B01 collection, Arch/Linux:** Stop the Trading Lab paper engine (or
do not start it); keep IB Gateway in PAPER mode and authenticated. Use a
private directory outside the repository, and do not run a smoke order:

```sh
# From the candidate repo checkout, with the appropriate local lab.toml:
uv run python tools/capture_ib_session.py --config lab.toml \\
  snapshot B01-readonly --out "$HOME/trading-evidence" \\
  --tickers SPY --wait 45 --days 14

# Replace SNAPSHOT_DIRECTORY with the printed private path from the preceding command:
PYTHONPATH=src python -m lab.kernel.paper_capture_bridge \\
  --config lab.toml --capture SNAPSHOT_DIRECTORY \\
  --ticker SPY --action-version v1 --b01-report
```

`SPY` is just an example: select a symbol already present in
`[universe]`. If IB Gateway is unavailable, the capture should fail rather
than fabricate evidence. Only report the **non-sensitive** checklist JSON and
errors after checking they contain no credentials or account numbers; never
paste the underlying `broker_*.json` files. Keep the original hashes and
captured data local for supervised acceptance. The next phase still needs
direct authenticated broker-session evidence and complete historical cursors.
Account scope, instrument version and paper port must match the local config.
The raw capture and its private provenance must never be published.

## Operator and privacy constraints

## Failed Gateway connection: private incomplete evidence

In the initial owner-operated PAPER acceptance run, the local Gateway API
accepted a connection, but the IBKR market-data and security-definition farms
remained disconnected (error codes 2103 and 2157). Nautilus aborted startup
after its 30-second data-client connection limit. Message 2107, by contrast,
merely describes an idle historical farm and is usually informational.

The capture command's `--wait 45` is an observation duration **after the data
client starts**; it does not override the broker connection timeout. Check
the Gateway/TWS PAPER session and IBKR data-farm status, local connectivity,
and instrument metadata before attempting another capture. Never bypass
data-client startup or source qualification in order to produce a report.

Every newly created private capture directory carries a
`CAPTURE_STATUS.json` document with one of `IN_PROGRESS`, `INCOMPLETE` or
`COMPLETE_UNVERIFIED`, and explicit false broker-authentication and
history-completeness flags. A failed node start retains the private incident
artifact marked `INCOMPLETE`, but cannot be loaded by the read-only bridge.
A normally returning capture requires all broker data files and a
`COMPLETE_UNVERIFIED` status, and remains entirely unqualified. The new
strict bridge requires the explicit status file, so old five-file captures
must be repeated to qualify for **inspection only**.

Original captures contain actual account IDs, order IDs, quantities, balances,
timestamps and other sensitive broker evidence. Keep them outside Git in a
private access-controlled directory; never attach them to an issue or
collaboration thread. Use the existing `lab.capture.sanitize` workflow and
mandatory independent manual review before sharing any *sanitized* export.
The bridge is designed for original private files; a sanitized export is not
a valid broker-custody assertion.

There is no operator requirement to run this feature yet. CI exercises
synthetic, fabricated account and order IDs only. Real IBKR paper acceptance
will be a separately supervised phase after protocol review.

## Release-blocking next requirements

1. Prove broker identity from the pinned paper session via authenticated
   provider state, not from self-reported JSON account IDs.
2. Obtain bounded, timestamped open **and closed** order histories and fills
   with reconciliation cursors, replay across disconnects, an authoritative
   position snapshot and explicit sequence/clock uncertainty.
3. Verify actual fees, cash, financing, corporate actions, futures/FX legs
   and margin against the independent double-entry ledger, without
   treating an order event cache as a complete historical feed.
4. On source gaps, time ambiguity, late fills, unknown open orders, or
   failed parity, quarantine the execution stream and prevent order
   resubmission or reservation release. Implement a separately approved,
   journaled state transition for recovery.
5. Validate against a credentialed, supervised paper broker with independent
   audit and failure-injection tests before enabling the first live-transport
   `submit` in the new OMS.

Passing unit tests or CI here closes none of the release criteria.
