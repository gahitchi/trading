# Operator acceptance: 97 Termux portable reference tests

**Recorded:** 2026-10-26 (Europe/Rome). This is a **user-supplied terminal transcript**, not signed remote attestation.
**Exactly tested commit:** `c8358952e05b3063f4766d7db06430be631b05d4`
**Branch:** `chatgpt/broker-reconciliation-oracle-20261026`.

Commands executed by the operator:

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/broker-reconciliation-oracle-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

Recorded output: `Already up to date`; **97 tests in 1.820 seconds, OK**; collaboration checker **113 files in the outbox tree, 37 threads, 0 errors**; `git rev-parse HEAD` precisely `c8358952e05b3063f4766d7db06430be631b05d4`.

That suite tested 16 synthetic reported-broker snapshot checks, 21 offline order lifecycle cases, 13 corporate domain checks, 12 corporate journal cases, 12 double-entry accounting checks, 9 mobile diagnostics, 9 reduce-only references and 5 offline simulator cases. The identity/position/order/execution reports were **synthetic**, not verified brokerage data. No broker connection or fund-moving operation was tested.

Exact-SHA hosted GitHub Actions workflow [40329353050](https://github.com/gahitchi/trading/actions/runs/40329353050) completed **success** including Python 3.13/3.14, real-clock harness, Ruff, package, Docker and collaboration checks.

**Limits:** Offline reference code cannot authenticate IBKR/exchange source, establish complete broker order/execution history or causal fence, mutate a real portfolio, release frozen inventory, reconcile cash/fees across instruments or authorize paper/live trades. All relevant F1/F3/F4/F6/F8 release gates remain unqualified. This evidence document is on a **later** descendant branch and not contained in the tested SHA.
