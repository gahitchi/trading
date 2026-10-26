# Termux portable Decimal reduction — operator-reported acceptance evidence

**Recorded date:** 2026-10-26 (Europe/Rome)  
**Repository:** `gahitchi/trading`  
**Tested immutable Git SHA:** `9fde8b74df8cf4afe9ea7513054b727e3fdac547`  
**Branch when tested:** `chatgpt/fundamentals-settlement-20261026`  
**Origin:** User-provided Termux command output, not an independently witnessed device session.

## Device/runtime evidence

- Environment: Termux on Android/aarch64.
- Packages: Termux Git 2.54.0; Python 3.13.13-1; both already installed.
- Source: fresh `git clone https://github.com/gahitchi/trading.git trading-portable-check`, followed by `git switch chatgpt/fundamentals-settlement-20261026` and `git pull --ff-only`; reported `Already up to date`.
- Test command: `PYTHONPATH=src python -m unittest discover -s tests -p 'test_portable_reduction.py' -v`.
- Result: nine named `ReductionSafetyTests` passed, `Ran 9 tests in 0.002s`, `OK`.
- Compilation: `python -m compileall -q src/lab/order_quantity.py tests/test_portable_reduction.py`; no diagnostic output; subsequent commands ran.
- Collaboration: `python tools/collab.py check`; reported `104 file(s) in this outbox tree, 30 thread(s) across branches, 0 error(s)`.
- Reported `git rev-parse HEAD`: `9fde8b74df8cf4afe9ea7513054b727e3fdac547`.

## What this does and does not prove

The submitted output provides **operator-reported native Termux evidence** that
the standard-library `Decimal` reference quantity oracle and its nine bounded
cases work on this Android/Python version: truncation to known lot step, both
position directions, fractional stubs, conservative unsettled reduction
reservation, wrong-side and invalid-state rejection, and mismatched split
versions.

This **does not** verify authenticated broker holdings, actual open orders,
broker-supported reduce-only semantics, true atomic check-versus-submission
behavior, NautilusTrader native-account parity, margin/collateral/corporate-action
settlement, real market data, Arch/Linux or Windows packaging, or any real-money
authority. The reference oracle is not on the production order submission path.

**Gate effect:** add to F3 *portable reference-arithmetic evidence* only.
Keep F1 blocked, F3 partial, F4 partial, F6 blocked and all independent release
criteria blocked. The evidence belongs to the **tested SHA**, not to every
later branch tip. The result is user-supplied rather than separately reproduced
by the model.
