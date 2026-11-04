# Portable Binance market capture (Termux / Debian ARM64)

This prototype records **public** Binance USD-M Futures combined WebSocket messages
to a crash-recoverable, uncompressed journal. It requires **no exchange keys**,
**does not submit orders**, and **does not import NautilusTrader**.

It is not a substitute for the production order-book recorder: bookTicker is
best bid/ask, not complete L2 depth. Liquidation forceOrder events are sampled
and must not be totaled as though the exchange delivered every liquidation.

## Termux smoke test (Debian proot)

On the branch containing this module, in the project's existing Debian venv:

    source ~/trading-venv/bin/activate
    python -m pip install 'websockets>=14,<17'

    PYTHONPATH=src:. python -m pytest \
      tests/test_market_journal.py tests/test_market_capture.py -v

    PYTHONPATH=src:. python -m lab.market_capture \
      --symbols BTCUSDT --seconds 60 \
      --out data/portable-market-capture

The existing market-research checkout is not modified by the recorder. Keep it
in a separate worktree if its branch differs.

## Durability and replay boundaries

- A single-process advisory lock prevents simultaneous capture/recovery in one
  output directory. Aborted segments are recovered on the next start before
  any live connections are opened.
- Completed .jsonl segments are written from unbuffered bytes, fsynced,
  renamed, and the parent directory fsynced. .jsonl.open files are never
  silently counted as completed. Recoverable complete newline-delimited records
  are retained in .recovered.jsonl, with .recovery.json metadata recording
  the count, discarded trailing bytes and SHA-256 digest.
- Receiver status events are recorded as stream: collector.status, with
  connection/disconnection/reconnect indications. These are not market events.
- No separate unbounded Python queue is used. The WebSocket client's
  max_queue=16 applies backpressure, but the data source may drop messages,
  disconnect or sample events. Forward studies must exclude sessions with
  unknown completeness.
- Each write has a wall-clock received_ns timestamp, **not** a synchronized
  exchange timestamp. End-to-end delay measurements need clock-offset sampling.
- Periodic file fsync is at most once per second during active writes, plus
  segment finalization. A hard kill or device power loss can still lose data
  between syncs or while the exchange sends messages that have not been read.
- The journal is deliberately **uncompressed while recording**. It can grow
  rapidly during active quote updates; run short sessions, monitor available
  storage and compress only finalized segments with independent integrity
  verification.

## Remaining qualification

Run offline tests on Termux, then controlled network disconnects, SIGKILL,
recovery without duplicate paths, high-load throughput, sustained CPU/disk
measurement, stale-feed alarms and strict cross-session gap reporting.
Compression/retention with verified archival checksums is not yet integrated.
Do not reuse the current crash holdout for signal fitting.
