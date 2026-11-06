# NautilusTrader sandbox order capture (paper/replay only)

**Status: implementation and synthetic integration tests, NOT qualified account reconciliation.**
Module: `src/lab/sandbox_capture.py`; hooks: `lab.fast.FastStrategy.on_order_event`.
Regression: `tests/test_sandbox_capture.py`.

This feature records order events from the NautilusTrader **sandbox execution
client**, or optionally from the same strategy during a synthetic backtest. It
does not import an exchange execution client, authenticate an exchange account,
use API keys, or authorize real-money orders. The source is a simulated engine,
not independent proof of an executed trade. `nautilus-sandbox` in the header
is a *local capture label* rather than an authenticated origin.

## Usage for supervised paper sessions

To opt in (choose a new ID per fresh sandbox account/run):

```bash
lab fast paper BTCUSDT --strategy taker --size 0.001 --capture-session paper-20261105-a
```

This uses **public Binance market data** plus **simulated** NautilusTrader
fills. Existing risk limits and kill switch still apply. A persistent
`results/fast/paper-orders-paper-20261105-a.jsonl` archive and matching
`.head.json` checkpoint are written under the configured results directory.
The capture is opt-in; paper runs without `--capture-session` continue their
prior reporting behavior. Never reuse a session ID to pretend that newly reset
sandbox balances belong to a previous session.

On any order-event archive failure, the strategy raises a runtime error, sets
the in-memory RiskManager into a persistent error/halt state, and writes the
normal `lab kill` file in halt mode; it does **not** attempt to exit positions
or fabricate missing order reports. If a kill-file write also fails, in-memory
risk remains HALTED, and independent operator intervention is required.

For a network-free code-path check, use an existing recorded-data catalog
with `lab.fast.backtest(..., capture_path=..., capture_session=...)`; its
actual NautilusTrader callback capture has automated integration coverage.

### Portable Termux/Debian validation

The raw archive, economic journal, and independent fill-reconciliation tests
use only the Python standard library and pytest. Run the following in the
existing Python 3.13 virtual environment without installing NautilusTrader:

```bash
PYTHONPATH=src:. python -m pytest \
  tests/test_profit_allocation.py \
  tests/test_profit_journal.py \
  tests/test_position_reconcile.py \
  tests/test_paper_evidence.py \
  tests/test_sandbox_capture.py -q
```

That portable subset currently contains **103** tests. The two tests
requiring `lab.fast` / `nautilus_trader` reside in
`tests/test_sandbox_capture_nautilus.py`. They are skipped when the optional
engine is unavailable in a normal local environment, but must execute in
the full GitHub CI suite: if the engine is absent in CI, they fail.
This separation prevents a missing optional native package from aborting
collection of the portable tests; it does **not** constitute external
NautilusTrader acceptance on the mobile platform.

## Evidence and validation contract

The append-only receipt archive contains:
- Exact event ID, strategy/trader, instrument and client order ID.
- Event/init times (nanoseconds), capture time, optional causation ID.
- For `OrderFilled`: execution-side, trade and venue order IDs, account ID,
  last fill quantity/price, fee `Money` string (or null when not supplied),
  liquidity side, currency, position ID, and reconciliation flag.
- Other supported order-lifecycle event kinds including submit, accept,
  reject, cancel, expiry, update, pending states and fill voids.

It does **not** claim to serialize every optional vendor-specific metadata
field or recover exchange-source timestamps not present in the event.
Serialization is non-lossy for the *selected* receipt fields, and no missing
commission is silently converted to zero.

Records are SHA-256 predecessor-linked and committed with a separately fsynced
head; append-only sequence numbers, event IDs and session identity are checked
on every read/write. Torn records, corrupted digests, duplicate event IDs,
missing or truncated checkpoint history block further use without an audited
external recovery process. Hashes are integrity checks, not cryptographic
authentication against a local attacker who rewrites all state.

`compare_flat_start_positions` is a **strict diagnostic** comparing signed
quantities of captured BUY/SELL fills with a caller-supplied independent
position snapshot. It is valid only when the capture spans a completely flat
starting account, one selected strategy/instrument and no outside trades. It
rejects repeated trade IDs (including price restatements) and fill void events
rather than counting unknown corrections as new trades. It does not derive
average entry, collateral, actual wallet equity, realized P&L, fees, funding
or liquidation. It cannot by itself establish an authentic snapshot.

## Dependencies still not met

1. Independently record account snapshots (wallet, orders, filled/remaining
   quantities, position inventory) with source identity, coverage and restart
   semantics; compare them against captured order-event streams.
2. Capture independent timestamped L2/mark observations and funding/cash
   settlements; verify quote causality, fee currency, contract multiplier,
   correction/restate rules, venue filters and order-state completeness.
3. Only after these checks, normalize selected, independently corroborated
   events into `PaperEvidenceArchive` and `ProfitJournal`. The raw
   `PaperOrderEventCapture` does **not** automatically post P&L.
4. Validate the whole risk and deployment workflow on real supervised paper
   sessions, including loss limits, partial fills, rejections, stale books,
   disconnects, kill switch, unexpected restarts and a qualified strategy.
5. Benchmark and move any required event capture onto a qualified
   asynchronous execution path before claiming high-frequency readiness.

Paper/demo only. Passing synthetic tests does not make this feature a
profitable strategy, an authenticated broker reconciliation or a live release.
