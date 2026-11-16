# Forward public market-data attempt ledger (research-only)

Stacked on draft PR #46. This milestone records **which public minutes were actually attempted**, not just which public GET responses succeeded. No account keys, orders, execution, P&L, protected BTC holdouts, or live funds.

## Independent acceptance used as input

On 2026-11-16 the user's Debian/Termux host passed 10 causal audit tests and reported 3 unique consecutive ETHUSDT forward minute bars, zero duplicates or internal missing minutes, 6 saved OI receipts and 2 saved funding receipts (9 known historical settlements). The as-of OI change of -0.07299512883932602% appeared unchanged across three consecutive price decisions. This cannot establish opening short pressure, a bearish continuation setup, historical funding completeness, or any profitable edge.

## Remaining collection-coverage problem

The prior collector saved immutable raw price/OI/funding responses and warned on TLS failure, but **a failed request could leave no durable attempt record**. Three price bars without internal gaps only prove continuity *inside those three observed timestamps*; the old audit cannot infer whether collecting was attempted or failed outside that span. It also cannot prove 24/7 uptime.

## New records

For every poll made through the CLI, the collector writes an exclusive immutable `<started_ns>-<index>.start.json` **before** the first network request, with the intended UTC minute boundary, actual local start time and expected OI/funding channels. After the attempted price / OI / funding operations it saves a separate immutable `.finish.json`, containing the local end clock, exact saved-response receipt references, bounded network-error **type names** (not exception contents), and any abort type. A crash after start but before finish leaves an orphan start **interrupted**, never a fabricated success.

The offline loader rejects unrecognized schemas, unsafe filenames, symlinks, unexpected trust flags, broken local clocks, outcome-only records, and finish references not matching original saved raw price/OI/funding captures. It does not claim the host clock is signed or the HTTPS response independently attested.

The coverage summary reports the counts of recorded attempts, completed/interrupted/aborted attempts, received/missing price receipts, scheduled/received OI and funding receipts, late attempts (>10 seconds past intended minute) and bars referring to the wrong scheduled minute. The counts are only for **recorded attempts**; they do not reconstruct past misses or guarantee continuous calendar coverage. A completed attempt may still lack one or more channels. Normal integrity failures always abort, and repeated price network failures continue to trigger the existing bounded halt.

## Termux independent local acceptance

```bash
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/bearish-forward-attempt-ledger-20261116

PYTHONPATH=src:. python -m pytest -q \
  tests/test_bearish_hybrid_forward_price.py \
  tests/test_bearish_hybrid_forward_audit.py \
  tests/test_bearish_hybrid_forward_attempts.py

PYTHONPATH=src:. python -m lab.bearish_hybrid_forward_collector \
  --root ~/trading-forward-evidence \
  --symbol ETHUSDT \
  --minutes 12

PYTHONPATH=src:. python -m lab.bearish_hybrid_forward_attempts \
  --root ~/trading-forward-evidence \
  --symbol ETHUSDT \
  --last 12
```

Keep the process active for future-minute collection; network/OS suspensions may cause missing or interrupted attempts and are intentionally not backfilled. The old successful three-minute collection predates this feature and will **not** be invented as past attempt records; its price/OI/funding receipts remain readable through the earlier causal audit.

## Still blocking a trading qualification

Independent exchange/clock source provenance; prospective continuous coverage and absent intervals; complete funding event and settlement checks; as-of eligible listings and trailing 30-day turnover; order-book BBO latency and partial-fill/cost models; margin/liquidation/wallet receipts; preregistered negative controls and protected holdout. All new attempt reports remain `qualified=False`, `actual_notional=0`.
