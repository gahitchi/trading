# Operator acceptance: no-credential multi-market preview (Arch/Windows)

**Branch:** `chatgpt/multimarket-architecture-20261025`  
**Scope:** D1 offline journal recovery + D2 EEA read-only public data + D4 local synthetic sandbox.  
**No authenticated broker accounts, payment details, API keys or real money are used here.**

Run these commands against an intentionally temporary workspace; **do not** point the journal or
app directory at an existing trading runtime. Do not run a live account or paper gateway
to perform this acceptance. The purpose is to detect packaging, filesystem, browser
and network differences not covered by hosted Linux CI.

## 1. Prepare this branch

```sh
git fetch origin
git switch chatgpt/multimarket-architecture-20261025
git pull --ff-only origin chatgpt/multimarket-architecture-20261025
uv sync --frozen --extra dev
```

## 2. Pure local simulator and durable recovery

Use a **new** journal path for every test to preserve idempotency and historical evidence.

```sh
mkdir -p ./results/manual-multimarket-check
uv run python -m lab.markets demo --journal ./results/manual-multimarket-check/example.sqlite3
uv run python -m lab.markets recover --journal ./results/manual-multimarket-check/example.sqlite3
```

Expect `mode = offline_sim`, `qualified_for_live_trading = false`, two synthetic
entry/exit fills and a valid `recovery_verified = true`. A second `demo --journal`
using the same path **must refuse a nonempty journal**. Recovery with a different
`--balance` **must refuse** to alter starting collateral. Never delete the journal
to evade a mismatch. Keep these account-bounded checks local.

## 3. Public market data — separate sources, not broker qualification

Only on a working internet connection in a supported EU domain:

```sh
uv run python -m lab.markets candles --instrument BTC-USDT --limit 20 --store ./results/manual-multimarket-check/candles
uv run python -m lab.markets observations --instrument BTC-USDT-SWAP --limit 20
uv run python -m lab.markets microstructure --instrument BTC-USDT-SWAP --depth 5
uv run python -m lab.markets history --instrument BTC-USDT --pages 3 --limit 100 --store ./results/manual-multimarket-check/candles
```

These two commands may fail if the public EEA endpoint, product, network or regional
entitlement is unavailable. **Record the failure exactly; do not switch to a global
OKX endpoint to make the test pass.** Completed candles and recent trades are not
a continuous replay feed. Mark and latest trade are two independent timestamps and
do not provide spread, best bid/ask, queue position or funding. The
separate microstructure command reports a bounded EEA REST book snapshot (best
bid/ask and ordered price levels) and an **indicative**, not settled, funding
rate. These feeds are not synchronized and cannot prove fills. The history
command accepts only strictly older page cursors, does not fill missing
intervals, and stores content-addressed Parquet files; it is not a complete
execution-quality data history.

## 4. Local browser synthetic sample

```sh
uv run lab app --directory ./results/manual-multimarket-check/dashboard --no-browser
```

Open the localhost URL printed by the app. Before providing any paper-broker
configuration, choose **Try crypto without a broker** and run the default sample.
Confirm that a clearly labelled synthetic position, equity, fees, and no-qualified-
strategy message appear, with no attempt to connect to IB Gateway. Also test
**Inspect public crypto market data**, choosing a swap instrument. It must
show the separate book and indicative-funding timestamps, deny requests without
an internet connection rather than fabricating a quote, and never activate
the paper broker, a strategy, or an order path. The main existing
IBKR paper wizard is deliberately separate; pressing **Start paper trading** is *not*
part of this acceptance.

## 5. What to report

Provide the command that failed and the terminal error text; for UI problems, provide
your OS/desktop, the displayed local error and a screenshot of the sandbox section.
Do **not** send secrets, account identifiers, credentials, private keys, raw broker logs,
or a production journal. Include the current branch commit SHA and whether
`uv run python -m lab.markets recover ...` accepted your newly created journal.

**Acceptance boundary:** none of these steps qualifies active demo orders or automatic
real-money trading. A separate authenticated and supervised OKX EEA demo acceptance
plan is needed before D3 can be implemented or approved.
