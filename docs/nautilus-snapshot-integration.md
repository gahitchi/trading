# NautilusTrader sandbox account snapshot integration — supervised only

This module links **actual NautilusTrader local sandbox cache state** to
the existing portable, append-only account snapshot journal.
It does not retrieve an independent authenticated broker statement or
qualify a trading strategy. All execution remains paper/synthetic.

Modules:
- `lab.nautilus_snapshot`: cache adapter without native imports.
- `lab.paper_account_snapshot`: strict historical source-prefix replay.
- `lab.sandbox_capture`: durable simulated order-event stream.
- `lab.fast.FastStrategy`: optional guarded baseline and timed capture.

## Supervised usage

Example (Binance **public market data**, local sandbox orders):

```bash
lab fast paper BTCUSDT --strategy taker --size 0.001 \
  --capture-session session-20261106-a --capture-account-state
```

Do not reuse a session ID for a new sandbox account. The CLI requires
`--capture-session` if `--capture-account-state` is enabled.

Two linked archives are created under the configured `results/fast`:
`paper-orders-SESSION.jsonl` and
`paper-account-SESSION.jsonl` (each with a durable `.head.json`).
At strategy startup, the producer requires one unambiguous local
sandbox account, a new empty order-capture log, zero cached orders
and zero open positions. Nonzero initial exposure fails closed. The
startup condition is a local simulation preflight, not proof that an
external venue has no positions.

On each monitor timer (`report_s`, normally 5 seconds), the producer
reads the sandbox cache's complete order history, all open positions
and every currency-specific AccountBalance, then persists a source-
bound account snapshot. A final replay snapshot is also supported via
`lab.fast.backtest(..., capture_path=..., capture_session=...,
snapshot_path=...)`, and is exercised in the full CI suite. The
snapshot producer is optional and runs **synchronously** during the
timer, not on the Rust order hot path. Performance benchmarking and
isolation are still required before claiming fast execution.

The adapter obtains signed positions from native fixed-point
`position.quantity.as_decimal()` and the LONG/SHORT side, **not**
`position.signed_qty`, which can be exposed as float in some
NautilusTrader builds. It reads exact
`AccountBalance.total/free/locked` Money decimals; no binary
float arithmetic is used to reconstruct cash or quantities.

Order callbacks use Nautilus `event.ts_init` (the engine's
initialization timestamp) to anchor receipt order in the same
timeline as strategy snapshots. They do **not** re-enter the
Rust-backed strategy clock from inside an event callback, and they
do not claim this event-init time is a separately measured external
network receive time. Unix epoch nanosecond zero is valid in synthetic
backtests and is preserved.

A local account snapshot can reject:
- missing/changed account, nonflat startup and foreign instrument exposure;
- mixed-currency, missing, nonfinite or arithmetically inconsistent balances;
- order IDs not present in the hash-verified receipt prefix;
- duplicate/replayed fills, canceled orders erroneously reported open,
  missing or reordered source events;
- corrupted, truncated or contradicted durable snapshots.

A capture/write/baseline error HALTs the strategy risk manager and,
if configured, writes the persistent kill file; it does not invent
fills, silently restore historical evidence or liquidate unknown
inventory.

## Qualification boundaries

`source_authenticated=false`, `risk_ready=false`,
`order_state_complete=false`, `equity_reconciled=false` and
`margin_qualified=false` remain mandatory report values.

The cache belongs to the *same local engine* that generated simulated
fills. Comparing that cache with its own order callback log is useful
for integration, **not** independent proof of an external fill,
wallet balance, margin or true account equity. Order lifecycle data
does not yet guarantee fully authoritative remaining quantities;
we do not qualify outstanding-order completeness.

Next tasks:
1. Archive and cross-check an independently timestamped sandbox
   account report or separate execution-client reconciliation source.
2. Validate complete order-lifecycle and remaining-quantity semantics,
   unfilled/pending/canceled orders, overnight positions, restarts,
   partial fills, margin and funding.
3. Add independent market marks and settlement evidence before
   converting source receipts into `PaperEvidenceArchive` and
   `ProfitJournal`; keep paper-only funding and fee currencies exact.
4. Benchmark event I/O, snapshot timer stalls, kill-switch behavior,
   disconnects and recovery. Only after these steps connect
   qualified net equity and capital reserves to trading risk gates.

For portable Termux regression testing:

```bash
PYTHONPATH=src:. python -m pytest \
  tests/test_profit_allocation.py tests/test_profit_journal.py \
  tests/test_position_reconcile.py tests/test_paper_evidence.py \
  tests/test_sandbox_capture.py tests/test_paper_account_snapshot.py \
  tests/test_nautilus_snapshot.py -q
```

Native `tests/test_sandbox_capture_nautilus.py` runs automatically in
the GitHub CI environment with the pinned NautilusTrader package.
The project is not authorized to send real exchange orders.
