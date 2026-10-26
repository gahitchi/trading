# Offline recorded L2 data evidence: integration checkpoint

**Branch:** `chatgpt/market-data-evidence-20261026`  
**Scope:** immutable-byte checks for independently pinned operator-recorded OKX standard `books` WebSocket frames, conservative book reconstruction, and read-only delivery through the **existing** NautilusTrader `DataActor`. D1/D2 remain partial; D3 remains blocked.

## Contract

- Only local, pre-recorded NDJSON is accepted. Each exact row has `received_ns` (local observation nanoseconds) and `frame` (the original standard `books` snapshot/update envelope). A capture must start with a snapshot; every subsequent frame must pass strict sequence, exchange/receipt timestamp, instrument, level, and uncrossed-book checks.
- A caller supplies an **independently obtained** lowercase SHA-256 of the *original bytes*. A hash calculated from an untrusted recording and then presented alongside it offers no independent authenticity. The verifier checks file type, symlink refusal, byte bound, terminal newline, duplicate JSON keys and parse type ambiguity.
- Offline verification does not retain intermediate depth by default. `retain_books=True` retains at most 256 reconstructed books or fails closed. The retained sequence may be converted by `build_causal_tape` to native `QuoteTick` events and delivered to the **existing** read-only Nautilus `DataActor`. No second OMS, trading strategy, allocator or order permission is created.
- Original receipt time is kept as native `ts_init`; exchange time is `ts_event`. One book is not proof of complete market history or actual order queue position. No authenticated exchange stream was captured in this checkpoint.
- The **separate** fixed `run_native_l2_partial_parity` diagnostic exercises actual Nautilus synthetic depth-driven taker/maker partial fills with independent Decimal accounting. That hardcoded fixture is not the operator recording and is not trading evidence.

## Inspect a recording without credentials

```sh
uv run --no-sync python -m lab.markets verify-book-capture \
  --input /absolute/path/to/capture.ndjson \
  --sha256 <independently-recorded-64-char-hex-digest> \
  --instrument BTC-USD-SWAP
```

The output must keep `source_authenticated`, `feed_complete`, `market_data_execution_qualified`, `broker_execution_enabled` and `qualified_for_live_trading` false. Use a **synthetic fixture** for tests, never private production or account recordings.

## CI evidence required before integration

```sh
uv run --no-sync pytest -q tests/test_markets_book_capture.py tests/test_markets_native_l2_parity.py tests/test_markets_nautilus_runtime.py
uvx ruff check src tests tools
uvx ruff format --check src tests tools
uv run --no-sync pytest -m "not llm and not harness" -n auto
uv run --no-sync pytest -m harness
python3 tools/collab.py check
```

Do not describe these commands as completed without an explicit successful runner result. GitHub Actions also builds the offline sample, package and Docker image. Reviewers must compare the actual feature-branch check results, including both Python 3.13 and 3.14.

## Blocks to external demo qualification

Independent acquisition and custody of venue streams, region/account entitlement, feed gap closure and cross-source provenance; authenticated demo execution and persistent broker order/collateral reconciliation; venue-true funding settlements and liquidation accounting; order-book fidelity and stress calibration; supervised multi-session acceptance. The mock collateral and built-in synthetic depth scenario cannot close these gates.

**Never switch to live real-money routing or weaken the existing US equity paper-only guards on the strength of this evidence.**
