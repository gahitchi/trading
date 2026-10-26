# Native Android/Termux offline reference simulation — operator-reported acceptance

**Checkpoint:** 2026-10-26 at 12:23 (device local time, Europe/Rome).
**Host:** Android 17 on aarch64, native Termux, Python 3.13.13 (from previous device report and screenshot).
**Checked branch:** `chatgpt/termux-offline-20261026`.
**Tested code revision:** inferred `0cf7fa412ccd9cf41ae33e2851aca5930b95e110` from its then-current GitHub branch head and operator output `Already up to date`. **The latest operator transcript did not itself include `git rev-parse HEAD`**; retain this inference explicitly rather than presenting the SHA as locally printed.
**Evidence provenance:** operator-provided shell transcript and mobile-browser screenshot; not an independently controlled device or a signed attestation.

## Observed results

1. `git fetch origin`, branch switch, and `git pull --ff-only` succeeded and reported `Already up to date`.
2. `PYTHONPATH=src python -m lab.mobile check`: **23 tests passed** in **0.735 seconds**, with `OK`. Nine prior pocket diagnostics, nine lot-step/corporate-action reference tests and five new reference-engine tests all passed.
3. `PYTHONPATH=src python -m lab.mobile demo`: report identified `mode=offline_sim`, `instrument=SIM:BTC-USD-LINEAR`, `input_kind=synthetic_fixture`, `market_data_validated=false`, `nautilus_engine_used=false`, `broker_connection_enabled=false`, `execution_authority=NONE_OFFLINE_ONLY`.
4. The synthetic scenario completed with **six trades observed, three minute bars, two simulated fills, no rejections, zero pending orders and flat ending position**. Balances: starting capital 1000 USD; ending balance/equity `999.670105`, realized P&L `-0.2300`, fees `0.099895`, funding `0`. The explicit conservation arithmetic `1000 - 0.2300 - 0.099895 = 999.670105` is consistent; it is still **not** independent native Nautilus account parity.
5. `PYTHONPATH=src python -m lab.mobile serve --port 8787 --demo`: user browser screenshot shows the labelled synthetic fixture and results, disabled broker execution; server recorded `GET / HTTP/1.1\" 200` at **12:23:44**.
6. `PYTHONPATH=src python -m lab.markets demo --side buy --journal ~/trading-lab-pocket/offline-demo.sqlite3` completed with the same synthetic report (minus extra mobile metadata).
7. `PYTHONPATH=src python -m lab.markets recover --journal ~/trading-lab-pocket/offline-demo.sqlite3`: `recovery_verified=true`, `journal_events=17`, `external_execution_enabled=false`. Recovered balance/equity, position, fees, realized P&L, mark and fill count equal the prior report.

## Qualification / exclusions

**Passed on device:** original deterministic Decimal **offline** market simulation with synthetic observations, local read-only browser rendering, standard-library portable checks, durable local SQLite event-journal replay and recovery of that offline session.

**Not proved:** performance at trading frequency or Android sleep/network reliability; any real venue bid/ask, source-qualified mark, funding or fee schedule; actual IBKR/OKX connection; exchange matching/fill realism; native Nautilus Portfolio/Cache/Account parity; real corporate-action settlement; post-crash broker reconciliation or broker order authorization; positive alpha; F0–F8 institutional release signoff.

The `python -S` and no-NumPy-import tests passed within the **portable suite**, supporting the stdlib-only boundary, but the operator did not separately execute a standalone `python -S` command outside that suite. No credential or real-money trading was attempted.

**Gate effect:** add observed on-device portable/offline reference test evidence and deterministic journal replay coverage. Do not mark F1, F3, F4, F6, F7 or F8 complete from this observation. A later commit, including this documentation update, is a different candidate and requires corresponding checks.
