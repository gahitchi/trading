# Arch operator checkpoint — 2026-10-25 (credential-free)

This document records **operator-reported local acceptance evidence**, not
an independent deployment attestation or a production/demo-account qualification.

- Base source checkout: `chatgpt/multimarket-architecture-20261025`,
  `1e0c99c` (before the subsequent native Nautilus and HTTP robustness changes).
- Platform: Arch Linux desktop, Python 3.14.7, `uv sync --frozen --extra dev`.
- Focused risk, reconciliation, recovery and Arch installer unit suite:
  55 passed; no external broker used.
- `python -m lab.markets demo --journal ...`: fixture executed exactly
  two synthetic fills, no pending positions, USD 1000.050105 closing equity,
  total simulated fees USD 0.099895, no risk halt.
- `python -m lab.markets recover --journal ...`: successful exact recovery
  of 17 journal events and matching balances and positions.
- Reusing the **same** nonempty journal for a new `demo` run:
  rejected with `JournalError`, confirming the reset guard.
- EU public OKX spot candles, swap independent marks/recent trades,
  L2 REST book and indicative funding snapshot requests:
  read-only retrieval succeeded, with no external order routing.
- Historical 1m `history --pages 3 --limit 100 --store ...`:
  299 observations and zero observed missing minutes, written to an
  immutable dated Parquet catalog manifest. **No complete-history claim.**
- Local browser: offline crypto synthetic sample and separate read-only
  ETH-USDT-SWAP order book / indicative funding pane both worked.
  IBKR credentials were not required for either action.
- Port 8765 was already occupied by an unidentified loopback listener.
  `--port 8766` worked. Suspending the HTTP process with Ctrl+Z then
  resuming led to dropped browser connections and `BrokenPipeError` logs.
  The operator later stopped the isolated test server and confirmed
  shutdown. This motivated the subsequent M8 transport fixes.

## Boundaries

These are **first-machine smoke and regression outcomes** for the base SHA,
not proof of trading-model alpha, source completeness, genuine maker queue
fills, real demo permissions, authenticated reconciliation, restart under
power loss, full first-run installer behavior, or any real-money capability.

The native Nautilus integration and HTTP robustness commits following
`1e0c99c` must independently pass CI and operator acceptance before their
own machine-level gate can be closed. M1/M2/M8 remain partial.

No account names, credentials, host paths, trading secrets, or private
broker captures are included.
