# Termux F1 independent corporate-action reference: operator-reported acceptance

**Date:** 2026-10-26 (Europe/Rome)
**Tested SHA:** `a6a05fb2ec9cdb41725c1d7e0113ecf2862eed92`
**Branch:** `chatgpt/corporate-action-ledger-20261026`
**Test provenance:** operator-provided native Termux console transcript; the assistant did not remotely witness the device. Previous operator runs identify Android 17/aarch64 and Python 3.13.13; Python version was **not reprinted** in this particular run.

## Exact user command sequence

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/corporate-action-ledger-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

## Observed results

- Git reported the branch tracks `origin/chatgpt/corporate-action-ledger-20261026` and `Already up to date`.
- `lab.mobile check`: **46 tests in 1.071s, OK**, with every named test reporting `ok`.
- Corporate-action financial invariants: 13 tests, including long/short receivables/payables, zero recovery, delayed and partial settlement, stale availability, unsettled GTC orders, duplicate receipts, fractional entitlements, genuine zero basis and strict JSON Decimal parsing.
- Corporate journal/recovery: 10 tests, including short debts, writeoffs, journal hash tampering, accounting-state tampering even when hash chain is internally intact, account identity mismatch, append-before-memory-commit recovery, foreign market fill rejection, no NumPy/third-party imports and double-retirement prevention.
- Preexisting mobile diagnostics: 9 tests; reduce-only reference: 9 tests; offline simulator: 5 tests.
- `tools/collab.py check` reported **108 file(s) in this outbox tree, 33 thread(s) across branches, 0 error(s)**.
- `git rev-parse HEAD` printed exactly `a6a05fb2ec9cdb41725c1d7e0113ecf2862eed92`.

## Explicit limitation

A successful test of the **independent reference financial ledger** is not proof of true brokerage positions, account ownership, settlement payable custody, an actual short cover/bankruptcy distribution, or NautilusTrader native Portfolio/Cache/Account parity. The reference `ReconciledBook` accepts asserted verification booleans, not authenticated broker evidence. There was no connected broker, account, exchange, outside fill, or real-money order.

**Effect on F0–F8:** accept this SHA's Android portable financial-reference and deterministic-journal test evidence. **F1 stays blocked** pending native engine/account parity. F3/F4 broker execution and F6 demo qualifications remain blocked; no seven release criteria are satisfied solely by this result. Future commits require their own CI and, where relevant, operator acceptance.
