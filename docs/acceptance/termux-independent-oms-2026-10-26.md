# Operator acceptance — independent order lifecycle on Android Termux

**Operator date:** 2026-10-26, Europe/Rome  
**Exact tested SHA:** `60bba53718922e984c3e4fb1b246064968278b1f`  
**Tested branch:** `chatgpt/independent-order-lifecycle-20261026`  
**Evidence:** operator-provided full Termux transcript. This was not remotely witnessed; no cryptographic device attestation or actual broker was involved.

### Commands and results

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/independent-order-lifecycle-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

- `git pull --ff-only`: `Already up to date`.
- `lab.mobile check`: **Ran 80 tests in 1.557s; OK**.
- Included 25 corporate-action and journal tests, 12 double-entry financial invariants, **20 independent order-lifecycle tests**, 9 mobile diagnostics, 9 reduction-safety tests and 5 existing offline-simulator tests.
- The new order tests exercised conservative pending cancels, late fills, duplicate execution identities, account/instrument version rejection, SQLite transactional journal-head checks, concurrent stale processes, deterministic restart and `python -S` imports without Nautilus/scientific dependencies.
- `tools/collab.py check`: **112 file(s) in this outbox tree, 36 thread(s) across branches, 0 error(s)**.
- `git rev-parse HEAD` printed **exactly** `60bba53718922e984c3e4fb1b246064968278b1f`.

### Hosted CI qualification

At inspection, exact-SHA workflow [40327115889](https://github.com/gahitchi/trading/actions/runs/40327115889) had passed packaging, collaboration and Docker but **failed Ruff formatting**, while Python 3.13/3.14 were still finishing real-clock harness. Thus **do not call this SHA green hosted CI** despite successful operator tests. Formatting corrections are on the *descendant branch* `chatgpt/broker-reconciliation-oracle-20261026`; they are not part of the tested SHA, and require independent CI.

### Limits and gate effect

The portable independent OMS is an unauthenticated, **OFFLINE_ONLY** journaled reference reducer. It has no broker transport, account-scope identity verification, evidence signature, sequence-fenced reconnect, actual account snapshot, cash/fee execution posting, full multi-asset reservations or real-money authority. A passing test of reported event handling is not custody/market or production proof.

The Android result is accepted as evidence for **reference financial/OMS arithmetic and local replay only**. F1 real corporate settlement, F3/F4 authenticated broker order reconciliation, F6 supervised external demo and F8 operational signoff remain blocked.

This note itself was committed *after* the tested SHA and must not be cited as if it had been included in that checkout.
