# D1 acceptance verification — linear accounting, provenance and adapter surface

Date: 2026-10-25  
Scope: `chatgpt/multimarket-architecture-20261025` exclusively  
Status: D0 design accepted; D1 **pure-math/checkpoint tests verified**, complete D1 release **NOT** accepted.

## Binding checkpoints

| ID | Evidence | Verified behavior | Status |
|---|---|---|---|
| D1-T1 | `tests/test_markets_accounting_transitions.py::test_transition_branches` | Long/short adds update quantity-weighted entry with no realized PnL | PASS |
| D1-T2 | Same | Partial/full long and short reductions realize only the closed position; residual entry stays constant or resets to zero on close | PASS |
| D1-T3 | Same | Crossing position zero in one fill closes the old sign completely and anchors new sign at the fill price | PASS |
| D1-T4 | `::test_transition_conserves_marked_equity` | With no fee or spread, realized + immediately revalued residual PnL = prior marked PnL, for both directions and a non-unit multiplier | PASS |
| D1-T5 | `::test_sign_flip_commits_one_fill_and_charges_fee_on_full_notional` | Offline simulator invokes *same* pure oracle and charges the fee on the entire crossing fill, not only its opening or closing component | PASS |
| D1-P1 | `tests/test_markets_provenance.py`; `tests/test_markets_provenance_isolation.py` | A crypto-only source edit changes crypto + runtime digests but not default equity `simulation_digest()` | PASS |
| D1-P2 | Same | Shared agent risk edit changes equity `simulation_digest()`; pure operator UI edit leaves evidence digest unchanged | PASS |
| D1-C1 | `python -m lab.markets.capability_probe` executed in Python 3.13 and 3.14 CI with lockfile | `nautilus_trader==2.0.0rc7`, OKX EEA + DEMO enums, execution config/factory exact export names | PASS |
| D1-C2 | Same | No importable pinned `nautilus_trader.adapters.alpaca` or `nautilus_trader.adapters.ctrader` | CONFIRMED D5 DEPENDENCY |

## Interpretation and limits

The exact exported OKX class names are `OKXDataClientConfig`, `OKXDataClientFactory`, `OKXExecutionClientConfig` and `OKXExecutionClientFactory`. A guessed alias `OKXExecClientConfig` is **not** exported. These results are import-surface checks, not proof of the OKX Europe demo's regional contract support or correct order lifecycle.

In this repository the implementation identity used by `research/approval.py` is `simulation_digest()`, not a function named `source_digest()`. The change to `provenance.py` itself can cause a **one-time historical digest mismatch**. Previously approved equity evidence is **not grandfathered**; mismatches must force fresh review/approval. After that migration, future crypto-only source edits do not disturb equity fingerprint identity.

The accounting oracle currently powers the **offline** simulator; the real Nautilus AllocatorAgent/RiskAgent/ExecutionAgent integration is D2, deliberately not claimed here.

The latest completed D1 regression suite at commit `b9d04fbd` reports 613 pass / 1 fail on both Python 3.13 and 3.14; the sole failure is the **inherited invalid collaboration-history thread G014**, not a trading test. Lint, package, Docker and collaboration CI check jobs pass; full CI is not green. The additional provenance-isolation tests on later commits are pending a final complete CI run at the time of writing.

## Remaining blocked requirements

D1 release completeness: crash recovery/reconciliation, realistic bid/ask order book fills, funding and mark feed authenticity, replay evidence, cost/fee tier calibration, and simulator/agent parity. D2: integrate shared oracle with existing Nautilus agents. D3: real OKX EEA demo account acceptance and adapter order lifecycle. D5: independently qualify updated Nautilus distribution **or** thin Alpaca v2/cTrader Open API adapters, preserving typed capabilities. D6: separate live-money authorization; live execution remains disabled.

Do not merge into `main` or modify existing IBKR paper-only guards based solely on these checkpoints.
