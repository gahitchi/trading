# Trading Lab — independent-engine institutional roadmap (F0–F8)

**Owner decision: 2026-10-27.** This is the staged development roadmap;
**it does not authorize paper orders, live trading, or software release**.
The machine-readable contract is [f0-contract-map.json](f0-contract-map.json).
The architecture decision is [adr-independent-engine-2026-10-27.md](adr-independent-engine-2026-10-27.md).
The scoped first viable product is
[mvp-f1-acceptance-cutline-2026-10-27.md](mvp-f1-acceptance-cutline-2026-10-27.md).

## Nonnegotiable principles

- Trading Lab owns its canonical economic events, accounting, portfolio,
  decisions, risk controls, and order lifecycle. **NautilusTrader is an
  optional external comparison oracle**, not the canonical dependency.
- Follow F0→F1→F2→F3→F4→F5→F6→F7→F8 in order for **qualification**.
  Implementing code in parallel does not waive dependency or sign-off gates.
- An institutional-quality MVP means a **minimal supported surface**, not
  less rigorous evidence. Unsupported instruments, currencies, account
  types, ambiguous broker events or unexplained cash must fail closed.
- AI, multi-agent exploration, broad broker coverage, sophisticated
  optimizers and feature-heavy dashboards are postponed until the
  deterministic MVP passes its stage gates. No promise of profitable
  strategies is made.
- Keep research, simulator, external PAPER and real-money capabilities
  separate. The currently approved runtime policy is **PAPER_ONLY**.
  Strict reconciliation, kill-switch, release and dependency security
  requirements are not relaxed.

## Qualification map

| Gate | Deliverable | Required exit evidence |
|---|---|---|
| **F0** | Reproducible foundation | Exact source and dependency provenance, repeatable CI and independently attested Arch candidate |
| **F1** | Economic conservation | Exact typed source events; FIFO/fees/splits/settlements; durable replay; external cash/position/order evidence and independent arithmetic review |
| **F2** | Causal point-in-time data | Versioned source/availability timestamps, missing-data refusal, instrument/universe membership history |
| **F3** | Typed decisions and risk | Bounded exposure including uncertain orders, deterministic policy and strict kill/reduce-only invariants |
| **F4** | Order state lifecycle | Durable accepted/part-filled/filled/canceled/rejected transitions, matching/simulation fidelity and restart/reconcile hard-stops |
| **F5** | Scientific validation | Reproducible experiments, holdout, realistic costs and selection-aware inference |
| **F6** | Authenticated external PAPER | Real broker observation, B01–B10 acceptance, account/order identity, provenance and independent financial parity |
| **F7** | Operator deployment | Reliable install, safe upgrades, visible halts, recovery and verified operational soak |
| **F8** | Independent release | All seven release gates independently approved against one exact reviewed candidate |

**Current qualification: all stages remain BLOCKED until their formal exit
criteria are independently satisfied.** A local test pass is implementation
evidence, not formal certification.

## MVP build cutline

Start with one USD cash-account trading scenario, one explicit historical
data source, one deterministic strategy, a conservative risk policy,
own-engine simulated execution and exact reproducible reporting.
External PAPER is a separate, supervised F6 qualification. Additional
currency/venue/account features must undergo additional evidence checks.
The existing NautilusTrader-based Python application is a legacy
implementation reference, not the replacement engine's target contract.

## Ownership and integration

The collaboration protocol governs branch ownership: ChatGPT commits to
`chatgpt/*`; Claude reviews and integrates into `main`. Merge this
documentation independently of experimental F1 code, preserving full CI.
Neither a review nor a merge changes any gate to QUALIFIED automatically.
