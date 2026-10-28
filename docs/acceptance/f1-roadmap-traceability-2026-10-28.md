# F1.1–F1.6 exact roadmap traceability (Python independent economic candidate)

**Date:** 2026-10-28. **Branch:** `chatgpt/f1-roadmap-coverage-20261028` (stacked above PR #8).
**Result:** DEVELOPMENT CANDIDATE, formal F1 qualification **BLOCKED**. Runtime policy remains PAPER_ONLY.
**Source of requirements:** `docs/architecture/roadmap.md`, phase F1.

A feature that passes local tests is not independently validated economics.
Each row distinguishes written code and tests from outstanding evidence.

| Work item | Implemented code and test evidence | Remaining |
| --- | --- | --- |
| **F1.1.a** Immutable account/instrument/currency/lot/posting | Frozen Posting/Lot/JournalEntry/EngineFact/EngineSnapshot classes, explicit USD-account/US-listed equity/ETF manifest in `cash_scope.py`; economic ledger and scope tests | Generic account/currency/instrument identities remain validated strings rather than consistently distinct runtime types; independent security-master source not qualified |
| **F1.1.b** Bounded Decimal double-entry FIFO | `economic_ledger.py`: trap rounding, FIFO lot release, long/short replay, balanced postings; `test_economic_ledger.py`, `test_engine_events.py` | Actual broker fractional/fee rounding convention needs external validation |
| **F1.1.c** Realized, unrealized, fees, position/cash | Realized and commission postings in ledger; `reference_valuation.py` parses byte-bound complete USD marks with per-quote timestamps, age limit, signed long/short FIFO unrealized profit and independent rational marked-equity cross-check; `test_reference_valuation.py` | Reference marks are not executable prices or broker-authenticated quotes; unrealized P&L stays out of settled cash |
| **F1.2.a** Own typed economic facts | `engine_events.py`, `fill_input.py`, `settlement_input.py` implement own typed fills, broker receipts, settlement, split, cash-in-lieu and cash reversal | Native broker-source adapter remains separately qualified |
| **F1.2.b** Event identity, scope, bounded original bytes | Strict bounded sources, account/order/event ref and time, SHA-256 and chained SQLite custody; `economic_store.py`, source-binding tests | Source digest is not issuer proof or independent off-host signature |
| **F1.2.c** Reject duplicate, contradictory and fabricated source | Content reparse on every load, unique IDs, time/order fences, decimal/JSON schema refusal; new mark/policy reparse prevents typed substitution with old source digest | Fabricated but internally consistent records require authenticated external provenance |
| **F1.3.a** Classified financial activity | Settled cash movements, dividends/withholding, interest, borrow/financing and commissions; immutable source-bound opposite cash reversal; settlement and correction tests | Exchange/broker-specific tax, financing, fee adjustments and trade busts excluded |
| **F1.3.b** Nontrade corporate actions | Exact split/reverse-split, fractional cash-in-lieu without fabricated liquidity, FIFO basis accounting; cash-in-lieu tests | Delisting, mergers, stock distribution, unreconciled rounding/entitlements excluded; no implicit approximation |
| **F1.3.c** Supported USD CASH matrix | `cash_scope.py` declares USD_UNLEVERAGED_CASH/US_LISTED EQUITY or ETF; rejects short inventory, overdrafts and borrow/financing at *every historical prefix*, including later-closed exposure; stricter `--cash-policy --require-cash-account-scope` in `f1_verify` and `f1_evidence`; `test_cash_scope.py` | Declared instrument manifest and account type still require independent broker attestation |
| **F1.4.a** Atomic original evidence | SQLite WAL + FULL, raw bytes, ledger/source/order SHA heads, one BEGIN IMMEDIATE for lifecycle+broker fill; `test_economic_store.py`, `test_order_lifecycle.py` | Unkeyed local hash root needs off-host signed/immutable witness |
| **F1.4.b** Exactly-once/retry/crash boundaries | Explicit head fencing, idempotent exact retries, rollback triggers, ACK/partial/cancel race tests, additive migration tests | Real kill/restart and broker-disconnect scenarios on Arch/operator environment need evidence |
| **F1.4.c** Deterministic restart/replay | Full event, posting, original source and order replay with read-only diagnostics and `f1_benchmark` measurements | **Internal performance gap:** repeated whole-history replay is superlinear; no validated sustained throughput or reviewed checkpoint path |
| **F1.5.a** Independently captured account/order/fill | Parses separately supplied complete account, position, broker order and per-fill receipt documents; source byte custody | Documents have not been independently authenticated and fixed-time-finality checked |
| **F1.5.b** Complete state/quantity/receipt parity | `order_observation.py`, `order_lifecycle.py`: ACK/partial/filled/canceled/rejected, open/requested/executed quantities, each receipt ref/quantity/SHA-256, cancellation ambiguity; adversarial tests | Additional native broker statuses (expiry/replacement) require separate contract if observed |
| **F1.5.c** Refuse unknown/stale/incomplete/fees | Reject incomplete scopes, missing/extra fills, overfills, unsupported fee/settlement, wrong account/instrument/timestamps | Actual independent statement completeness and broker fee/finality evidence outstanding |
| **F1.5.d** Exact deltas, no automatic rewrite | `lab.f1_verify`: machine-readable exact cash/position/order/state deltas; rational account projector; no ledger repair or execution authority | Rational alternative uses the same business facts; not a separate external oracle |
| **F1.6** Reproducible evidence, reviewer and exit | `lab.f1_evidence`: canonical SHA-256 source inventory and economic/order heads from one read transaction, standalone acceptance procedure in `docs/acceptance/f1-internal-acceptance-packet-2026-10-28.md` | F0 security signoff, authenticated independent broker sources, off-host evidence anchoring, Arch operator replay/throughput measurements, independent reviewer, exact-candidate CI/release gates |

## Unresolved blockers before marking F1 complete

1. The Python append path still replays the whole economic journal for each write. The synthetic benchmark exposes latency but does not make it bounded for production. An engineering throughput acceptance target and either qualified hardware envelope or safe checkpoint redesign are required.
2. The unleveraged USD cash profile is enforced only when explicitly requested by the offline diagnostic. It is **not** an authenticated broker/account execution gate; no broker integration exists here.
3. Formal F1.5 requires separately obtained, complete, independently reviewable and authenticated broker order/account/fill evidence at the same as-of boundary, including full fee classification. None can be replaced by internal fixture generation.
4. Other corporate actions, trade busts, unknown currencies, margin and unsupported instrument types are **explicitly out of the first USD cash MVP scope**. A future broker path requiring them must fail closed pending a reviewed contract.
5. Independently anchored journal roots, external financial calculation/oracle, real Arch restart/migration/backup evidence, F0 signoff, exact SHA full CI and reviewer approval are outstanding. The strict Docker High/Critical vulnerability gate must not be waived.

**Do not claim F1 qualified from the presence of this matrix, a local SHA-256 or isolated tests.**
The separate Rust engine also requires independent semantic parity and cannot inherit this Python result.
