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

## Verified archival (after the recorder stops)

Keep the original journal files while first validating compression:

    PYTHONPATH=src:. python -m pytest tests/test_market_archive.py -v
    PYTHONPATH=src:. python -m lab.market_archive \
      --out data/portable-market-capture

Every finalized .jsonl file receives a .jsonl.gz archive and a
.gz.manifest.json sidecar containing SHA-256 digests, byte lengths and the
record count. Compression is checked by streaming the compressed bytes back
and comparing their digest/length/count with the finalized original.

The default **never deletes raw data**. Only after inspecting the results and
confirming the archives are intact may the operator reclaim disk space:

    PYTHONPATH=src:. python -m lab.market_archive \
      --out data/portable-market-capture --prune-verified

Pruning is optional and explicit. The archiver re-verifies archive contents
and the manifest before deleting a raw file. It skips any .jsonl.open files,
refuses to run concurrently with a recorder in the same directory, and
restarts abandoned .gz.partial writes from the original finalized file.
If a completed gzip was left without its manifest, it is reconciled against
the original raw data. No compression runs in the network receive loop.
Long-term background compression, monitoring of available disk space and
retention rules are still unqualified.

## Controlled transport failure qualification

After the 27 baseline tests pass, run an intentional disconnect on **both**
WebSocket categories. Use an isolated output directory so the injected gap
cannot be mistaken for uninterrupted market history:

    PYTHONPATH=src:. python -m lab.market_capture \
      --seconds 90 --symbols BTCUSDT \
      --inject-disconnect-after 20 \
      --out data/portable-market-fault-test

The option closes each connection **once** after its 20th market event;
normal WebSocket reconnect and journaling paths must then restore recording.
The printed summary should report at least one reconnect per feed. The
journal must contain `collector.status` entries with
`fault_injected_disconnect`, `disconnected`, and a subsequent `connected`.
Any such gap invalidates cross-gap market signals and should be reflected in
quality gates. This is a controlled network-transport fault, **not** a
power-loss test or proof of lossless delivery. Omit the injection option in
normal recording.

## Offline continuity audit

After running the controlled 90-second disconnect experiment, audit the
actual files (including verified gzip-only archives when raw files have been
pruned):

    PYTHONPATH=src:. python -m pytest \
      tests/test_market_quality.py -v

    PYTHONPATH=src:. python -m lab.market_quality \
      --out data/portable-market-fault-test \
      --require-reconnect

The command exits 0 only if record structure and status transitions check out,
each connection logs an injected failure and a completed reconnection, and at
least one market event was recorded *after* the reconnect. The JSON report
includes per-feed connection-epoch counts and measured disconnect-to-reconnect
intervals. Nonincreasing recorded book/trade IDs, bad recovery manifests,
unclosed disconnects and unfinished segments are reported as audit failures.

**Important:** even a green audit does not establish lossless delivery or
continuity of exchange book data; every documented reconnect interval is a
gap unsuitable for continuous market-microstructure calculations. Aggregate
trade IDs can advance while the client is disconnected, and bookTicker is not
a replayable incremental order book.
