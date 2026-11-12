# Bearish hybrid — public USD-M daily candle archive acquisition (v1)

**Research/evidence collection, not an execution strategy or historical edge test.** This milestone extends the stacked draft candidate PRs #37–#41. It does not access the BTC holdout, private exchange endpoints, orders, or account balances. No trade outcomes are evaluated. It deliberately does **not** generate historical OI/publication timestamps, funding settlements, contemporaneous listing snapshots, or 30-day historical turnover.

## Source and trust levels

Binance documents its public archive at `https://data.binance.vision/` (see `https://github.com/binance/binance-public-data`). Each daily USD-M 1m ZIP has a companion `.zip.CHECKSUM` containing a SHA-256 and the archive filename. The collector constructs URLs from a validated uppercase non-BTC USDT symbol and one explicit UTC calendar day; arbitrary URLs and candidate-supplied hosts are not accepted. Real downloads require HTTPS with no redirects, a bounded response size and a UTC timestamp captured at collection time.

The collector checks that the retrieved ZIP SHA-256 exactly matches the *publisher-provided* companion checksum. This detects many accidental/truncation/transmission errors, **but it is not an independent publisher signature**: the checksum and ZIP come from the same domain, and both can change. A source manifest records hashes for the raw ZIP, checksum and uncompressed CSV, source URL, retrieval time, symbol, UTC day, 1,440 validated bars and explicit `independent_source_authenticated=False` and `contemporaneous_publication_verified=False` flags. Offline reading revalidates those hashes and the entire CSV. Synthetic callback acquisition is recorded as `source="injected-test-source"` rather than as official HTTPS evidence.

The historical archive is **retrospectively published**. A candle’s open/close time is not an attested date when the ZIP or its OI/funding contents were available to a trader. No OI or funding signal is constructed from this acquisition, and the receipt is not sufficient to set `funding_verified=True` or to infer exchange historical listing eligibility. Later, source collectors must supply independently reviewed publish-as-of evidence and contemporaneous turnover before the hybrid can be evaluated for qualified signals.

## Exact parser and storage gates

`lab.bearish_hybrid_acquisition.acquire_binance_usdm_day(root, symbol, day, boundary_ns=...)`:

1. Reject invalid symbols, BTCUSDT, unaligned/non-UTC calendar day cutoffs, dates that finish at/after the protected boundary, and unfinished days before any download.
2. Download the fixed USD-M daily 1-minute `.zip.CHECKSUM` and `.zip` with bounded byte counts. Reject unexpected redirects, missing/incorrect checksum filename and a mismatching SHA-256.
3. Read exactly one expected CSV member in the ZIP, with a 12 MB decompression bound, 12 expected Binance 1-minute columns, optional header, and safe UTC millisecond/microsecond timestamp normalization. Reject malformed OHLC/volume/trade counts, shifted or duplicated minutes, missing bars, incomplete/overlong UTC days and any unexpected ZIP members. Each complete UTC day must have **exactly 1,440 consecutive 1-minute bars**.
4. Persist the raw publisher `.zip`, exact `.CHECKSUM`, and receipt under `root/binance-usdm/daily-klines/<SYMBOL>/<YYYY-MM-DD>/`. Never silently overwrite differing old evidence; a conflicting archived daily file is a **hard stop** for independent investigation. Persist evidence without decompressing it to an executable or arbitrary filesystem path.
5. `load_captured_binance_usdm_day` revalidates the raw stored checksum, ZIP and receipt entirely offline and returns closed-minute `Bar` values **only**. These are not executable order or funded-trade records.

For real acquisition on a permitted **pre-protected**, independently selected ETHUSDT research day, a user-owned script can invoke:

```python
from datetime import date
from pathlib import Path
from lab.bearish_hybrid_acquisition import acquire_binance_usdm_day, load_captured_binance_usdm_day

# REPLACE these with a separately frozen, prospective plan's actual day and boundary.
day = date(2024, 2, 10)
boundary_ns = 1711929600000000000  # example only, 2024-04-01 00:00 UTC

receipt = acquire_binance_usdm_day(
    Path("data/public-archive-audit"), "ETHUSDT", day, boundary_ns=boundary_ns
)
bars, proof = load_captured_binance_usdm_day(
    Path("data/public-archive-audit"), "ETHUSDT", day, boundary_ns=boundary_ns
)
assert len(bars) == proof["rows"] == 1440
```

**Do not run this example to mine a new strategy result.** The dates are illustrative. Verify the existing frozen holdout cutoff, permitted symbol and research plan before retrieving any market source. Downloading/reading price bars is not a registered backtest. Register every subsequent candidate evaluation with `lab.research_family.register_family` and `lab.trials.trial_run` before calculating or inspecting returns. Original source checksums and collection logs should be independently mirrored to prevent tampering with local receipts.

## Evidence not yet collected

- USD-M OI measurements with original *publication/arrival times* (not guessed from candle timestamps); archived OI files may not preserve when historical samples became available.
- Funding event settlement and public availability records verified independently, fee/funding schedules, spot/perp historical listing and delisting eligibility, 30-day trailing USD-M and spot turnover without lookahead.
- Cross-symbol/control data in a precommitted trial family, subminute order-book latency/partial fills and exchange margin/liquidation data, plus paper wallet receipts.
- An actual prospective out-of-sample strategy experiment on never-inspected data. No positive expectancy, daily profit, or live deployment conclusion follows from this module.

## Validation

Run `PYTHONPATH=src:. python -m pytest tests/test_bearish_hybrid_acquisition.py -q` plus repository-wide `ruff check src tests`, `ruff format --check src tests`, `PYTHONPATH=src:. python -m pytest -q` and independent Termux acceptance. Synthetic ZIP tests do **not** perform network I/O and intentionally do not establish real data provenance.
