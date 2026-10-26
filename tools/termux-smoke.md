# Termux acceptance — portable quantity-safety smoke tests

**Scope:** Android Termux with standard Python and Git only. No trading account,
broker API, market-data subscription, `uv`, NautilusTrader, pandas, NumPy,
Docker, remote services or privileged permissions. Do not install large native
scientific dependencies just to claim these checks have passed.

A stock Android / Termux runtime is NOT a certified target for the pinned
NautilusTrader engine. These checks independently exercise a pure stdlib
Decimal reference and basic repository syntax/collaboration; they do not
qualify broker execution, market-feed integrity, live accounts, Arch/Windows
packaging, native Nautilus account parity or F0–F8 release gates.

## New isolated checkout in Termux

```sh
pkg install -y git python
cd "$HOME"
git clone https://github.com/gahitchi/trading.git trading-portable-check
cd trading-portable-check
git switch chatgpt/fundamentals-settlement-20261026
git pull --ff-only
git rev-parse HEAD
PYTHONPATH=src python -m unittest discover -s tests -p 'test_portable_reduction.py' -v
python -m compileall -q src/lab/order_quantity.py tests/test_portable_reduction.py
python tools/collab.py check
```

If the folder already exists, do not overwrite it or reset its history; use
another directory or a clean repository worktree. Avoid operating in Android's
shared-storage folder for executable projects.

Expected: the `ReductionSafetyTests` unittest suite completes with `OK`,
`compileall` exits zero, and the collaboration checker succeeds. Report the
full commit hash, test totals/errors and `python --version`/`uname -m`.
Do not paste personal account numbers, broker keys or production trade journals.

## Limits and interpretation

- A reference oracle evaluates candidate quantities with verified position,
  current lot step, all open orders and post-corporate-action version provided
  by an *assumed* reconciler. It never submits an order or authenticates those
  claims. Real execution additionally requires broker-proven snapshots,
  single-writer intent accounting and atomic order submission/reconciliation.
- A positive result shows platform-independent **arithmetic**, not that
  IBKR/OKX expose the same capabilities or adjust GTC orders identically.
- Full `uv sync`, `pytest` of the repository and the native LiveNode harness
  still run on supported Linux CI / Arch hardware, when available. Do not
  force-install or emulate unsupported engine binary wheels inside Termux to
  manufacture a green result.
