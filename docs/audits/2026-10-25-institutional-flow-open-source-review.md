# Trading Lab — institutional-quality application-flow and open-source stack audit
**Audit date:** 2026-10-25 · **Scope:** `chatgpt/multimarket-architecture-20261025` · **Status:** diagnostic roadmap, not a release certification.

## Executive verdict

The repo already has a serious **US-equities paper-research core**: one Nautilus event-bus trading actor stack, research process isolation, approval and holdout guards, a configurable portfolio allocator, initial-margin reservations against subsets of independent fills, broker reconciliation, and operator authority controls. These are reusable intellectual and engineering assets, **not a cheap copy**. The newly added `lab.markets` is still a **separate offline derivative-accounting reference** and public-candle reader. It has no real demo exchange executor, no Nautilus crypto actor bridge, no production research data catalog, and no GUI integration. Making a claim of multi-market production capability today would be inconsistent with the code.

**Strategic choice:** preserve the Nautilus engine and shared agents; build no second execution engine. Use selectively chosen, genuinely open-source libraries for *bounded*, reviewed responsibilities. Do not mistake many strategies, many AI models, or backtest speed for out-of-sample edge. No proprietary fund's secret algorithm is available to copy lawfully, and even a published institution-grade method requires independently tested data, costs, constraints and uncertainty. Incorporate **publicly documented** institutional methods with attribution and independent implementation or properly licensed upstream code.

**Release separation:** equity IBKR remains paper-only; crypto OFFLINE_SIM is a development reference; connected OKX EEA DEMO, a Windows/Arch first-boot crypto GUI, cTrader/Alpaca, and real-money routing are not qualified. `docs/release-criteria.json` and `docs/architecture/multimarket-release-gates.json` remain blockers.

## 1. End-to-end flow — current state, defect and acceptance condition

| Stage | Existing concrete path | Current contradiction or missing production behavior | Institutional acceptance condition |
|---|---|---|---|
| 1. Installation/onboarding | `src/lab/app.py:Controller.configure/start`, `src/lab/cli.py`, Docker/Arch/Windows launchers | Dashboard wizard requires IBKR host/port/account and starts US-equity daily paper node; new `python -m lab.markets demo` requires no credentials but is **not routed from the GUI**. The advertised zero-credential product is true for the standalone CLI only. | Both supported platforms, fresh/no account: choose OFFLINE_SIM in GUI, local data and strategy, explicit risk budget and Start; no IBKR/Gateway, no paid API, no hidden mode fallback. Installation and upgrade/rollback tested. |
| 2. Venue identity / permissions | `lab.config`, `lab.safety`, `lab.paper`, `lab.markets.domain.Capabilities` | Equity is IBKR paper (account validated). `Capabilities` currently expresses broad booleans, not per-product authenticated region, margin mode, instrument lifecycle, shortability, order-types, borrow/position mode. An EEA enum alone does not qualify X-Perps. | Authenticated capability proof keyed by account fingerprint+region+venue+product+mode; unknown denies, live credentials rejected on demo route, missing min lot/step/margin rejects, product/region delisting halts. |
| 3. Raw data + provenance | `lab.data`, `lab.intraday`, `lab.markets.okx_public`, `lab.markets.minute[_batch]` | Existing equity downloads use Yahoo/IBKR with documented survivorship, timestamp and history limitations. The new crypto path reads only **confirmed public OHLCV**, not independent trade, quote, index, mark, funding and book updates; CSV marks are unverified synthetic fields. | Append-only timestamped raw observations, feed/source IDs, sequence/revisions/checksums, gap/clock-skew monitoring, UTC exchange+receive times, per-instrument contract parameters as-of event time, immutable Parquet partition manifests. Replay qualification on a held-out vendor capture. |
| 4. Quant research and falsifiable hypotheses | `lab.experiments`, `lab.stats`, `lab.research.isolation`, `lab.research.approval`, `lab.research_state` | Strong trial ledger, holdout guard, PBO, DSR, stationary bootstrap and provenance in equities. They do **not** currently cover 24/7 crypto datasets or mark/funding economics. Broad automated parameter search without effective-trials accounting would reintroduce p-hacking. | Research protocols specify economic intuition, universe, train/test partitions, embargo by label horizon, capacity/costs, baseline, negative controls, selection budget, untouched prospective holdout and full selection exposure. A trial must be recorded *before* evaluating the next candidate. |
| 5. Signal and policy generation | `lab.signals`, `lab.agents.SignalAgent`, `lab.agents.CrossSectionalAgent`, `lab.strategy_bank` | Existing actors consume exchange-specific sessions/US market close bars. No 24/7 crypto signal timetable, funding event semantics or validated cross-asset strategy policy exists. Regime bank is explicitly experimental and unqualified as a whole. | Reuse the same actors through normalized Nautilus instruments; signal decision horizon separate from tick/1m execution monitoring; same strategy and event semantics in replay and demo; failed edge gates -> **ABSTAIN / CASH**. |
| 6. Portfolio construction | `lab.agents.AllocatorAgent`, `lab.allocation` | Equal/inverse-vol/HRP exists for equities, but 24/7 crypto collateral denominations, cross-contract covariance, shared exchange equity, inverse/linear products and concentration need a separate *instrument adapter*, not a second allocator. | A common constrained optimization interface respecting forecast uncertainty, position/sector/coin concentration, long/short/gross/net/margin, turnover, fees/funding, currency/collateral and market availability. Benchmark against equal and inverse-vol net of costs. |
| 7. Independent risk policy | `lab.agents.RiskAgent`, `lab.reservations`, `lab.margin`, `lab.markets.accounting` | Existing reservation examines order-fill subsets and equity Reg-T margin, but cannot be reused unchanged for perpetuals. D1 sim validates *filled* orders, not all possible outstanding-order combinations and authenticated venue schedules. Aggressive profile 30% drawdown is too permissive to silently inherit for a future small-capital account. | Cross-account risk source, per-product margin tiers, mark-vs-last separation, funding buffer, collateral haircuts, all pending fills, price-gap stress, max loss in currency, maximum liquidation probability warning, circuit breakers that cannot be raised by models/profiles. |
| 8. Order execution / microstructure | `lab.agents.ExecutionAgent`, Nautilus `RiskEngine/ExecutionEngine`, `lab.markets.simulator` | Stock node is IBKR-specific; the local crypto oracle executes against next **observed trade with adverse slippage**, not quote-side L2 depth. It is not a queue-aware matching engine. Historical 1m OHLCV cannot prove marketable/passive fills. | Venue-native order types and acknowledgements, client idempotency, bid/ask or L2 data, partial fills/cancel-fill races, smart reduce-only policy, latency+fees+funding, maker queue model tested against demo trade reports. Use upstream Nautilus execution algorithms rather than inventing an independent order stack. |
| 9. Ledger, restart and reconciliation | `lab.execution_store`, `lab.execution_state`, `lab.paper`, `lab.markets.journal` | Equity has durable pending-order recovery. Crypto journal now writes ordered source/mark/intent/fill evidence, but intentionally **refuses reopening a nonempty journal as a new simulator**; complete reconstruction/cross-venue reconciliation is not implemented. | Durable outbox, one writer lease, event-id/venue-id mapping, unknown-order freeze, replay from snapshot+events to deterministic same equity/open orders, independent venue transaction reconciliation, emergency flatten evidence even after power/network loss. |
| 10. Operator dashboard & observability | `lab.app`, `lab.ui`, `lab.notify`, `lab.authority` | Existing app is an IBKR-facing setup flow. New crypto CLI modes are not a unified UI. Existing health and risk alerts are focused on equity lifecycle. | Persistent and unmissable mode/account/venue/product indicator; live disabled until separate qualification; risk and margin before order, complete PnL decomposition, gap/latency/feed health, cancellation/unknown state, single-click halt with explicit certainty level. |
| 11. Testing, delivery, licensing | GitHub Actions, `tests/`, wheel/Docker/launchers, provenance digests | CI packaging/lint/Docker and trading tests run; the Python CI job's shallow Git checkout hid cross-branch G014 history, producing a false collaboration-thread failure. The dedicated collab CI job using full-history checkout passed; Python test checkout has now been changed to fetch full history (latest verification pending). A new experimental feature must not falsely inherit equity release qualification. Windows security parity for Linux seccomp worker is not demonstrated. | Unit/property/integration/recorded-venue contract/harness/soak, adversarial data and restart tests; lockfile+SBOM+license review; test supported Arch/Windows from a clean install, restore from backup, and no real-money code path during D1-D5. |

**Highest-risk gap:** the product currently has **two user experiences** (IBKR dashboard vs crypto terminal demo) and one mature **equities agent stack** versus a tiny crypto accounting harness. Merge the *behaviors*, not the execution engines: adapt instruments, market events and venue account capabilities into existing Nautilus actors.

## 2. Defects directly found and corrected in this audit

All fixes stay on `chatgpt/multimarket-architecture-20261025` and preserve the existing paper-only guards.

- **F-MM01 (execution economics):** a market event's participation budget was independently available to every pending order. `simulator.on_trade` now shares its observed-trade participation budget across all orders; regression covers two partial orders and the next tick.
- **F-MM02 (input state):** an invalid off-tick trade could poison duplicate-ID state before validation. State now changes only after basic trade-price validation and durable event append; regression proves valid subsequent replacement.
- **F-MM03 (event vocabulary):** market trades were logged as `kind=mark` alongside independent collateral valuation marks. New `trade` kind separates valuation and execution provenance.
- **F-MM04 (event-time):** a delayed trade's receive clock was used to seal the prior UTC minute, rejecting later valid trades in the next minute. New minute sealing uses the next interval's event-time boundary; overdue/late events still fail closed under explicit watermarks.
- **F-MM05 (journal mutability):** SQLite events could be edited/deleted accidentally despite append-only wording. DB UPDATE/DELETE triggers now block ordinary edits, with a test proving hash-chain detection after privileged trigger removal. Still NOT externally notarized/tamper-proof.
- **F-MM06 (bad public candle values):** nonfinite OHLC/volume and an unexpected provider response field could crash generically or silently be accepted. Public data is now strictly validated and fails closed on schema drift.
- **F-MM07 (replay precision):** timestamps came via floating-point `datetime.timestamp()`, and duplicated CSV headers could overwrite earlier price/mark fields. Replay uses integer UTC timedelta arithmetic and exact five-column CSV schema.
- **F-MM08 (mark price precision):** a mark valuation was constrained to the executable price tick, incorrectly rejecting real mark/index price precision. Execution trade tick requirements remain, but valuation marks permit finer precision.
- **F-MM09 (distribution parity):** include the synthetic replay fixture in the CI wheel manifest check and streaming-vs-vectorized bar regression.

Each is intentionally bounded to **development simulation / data tooling**, not an authorization for connected exchange orders.

## 3. Accepted open-source dependencies (evidence and integration strategy)

Core design goal: **excellent quality, free default, true open-source licensing, maintained upstream, minimal duplicative runtimes**. Having source visible or a free trial is not synonymous with OSI open source.

| Project | License (reported upstream) | Appropriate role | Decision |
|---|---|---|---|
| NautilusTrader | LGPL family (project's current pinned core), Rust/event-driven | Sole backtest/paper/future execution and risk event backbone; native algorithms and Parquet catalog | **KEEP as the single engine**. `2.0.0rc7` is still a prerelease; independently qualify a production release before any real money. |
| Apache Arrow / PyArrow | Apache 2.0 | Normalized immutable analytical columns, Parquet and in-memory interchange | **KEEP** (already in lockfile); no generic homegrown columnar store. |
| Polars | MIT | Fast, lazy scan, joins, factor and minute-bar feature engineering across 24/7 years | **RECOMMEND optional research dependency**, benchmark against NumPy+Arrow before making mandatory. |
| DuckDB | MIT | Zero-server local query layer over Parquet experiment and data catalogs | **RECOMMEND optional research dependency**; not order journal, not trading OMS. |
| skfolio | BSD-3-Clause | Portfolio covariance/shrinkage, cross-validation and constrained optimization comparisons | **EVALUATE for research-only portfolio candidates**; do not let it replace Nautilus risk actors; measure build/solver cost. |
| PyPortfolioOpt | MIT | Public Markowitz, Black–Litterman, shrinkage and HRP references/baselines | **EVALUATE** as an independently tested calculation provider, not second live agent framework. |
| Microsoft Qlib | MIT | Optional ML alpha/factor workflows and model benchmarking | **RESEARCH OPT-IN** only; broad ML search must enter full trial ledger and pass untouched holdout, no default heavy install. |
| Optuna | MIT | Experimental tuning under fixed budget and nested/purged out-of-sample validation | **RESEARCH OPT-IN**, subject to statistically charged trial exposure; avoid closed-loop overfitting. |
| Hummingbot | Apache-2.0 | Venue connector and inventory/market-making design reference | **REFERENCE ONLY / possibly reuse a small licensed module** after compatibility checks; do not launch a competing Hummingbot execution runtime beside Nautilus. |
| QuantConnect LEAN | Apache-2.0 | Independent algorithm/replay ideas and conceptual comparison | **REFERENCE / secondary cross-check**, not a second production execution core. |
| VectorBT public community edition | Apache 2.0 **with Commons Clause**, a source-available selling restriction | High-performance research inspiration, but not unrestricted OSI open source | **DO NOT BUNDLE as a genuinely open-source default**. Favor NumPy/Numba/Polars + Nautilus backtests. |
| Proprietary/paid quant SaaS, brokers' private strategy code | Varies; not free/open by default | Unverifiable internal edge, restrictive licensing or paid hosted dependency | **NO MANDATORY DEPENDENCY**. Do not copy trade secrets or nonredistributable code. |

Upstream reference entry points:
- https://nautilustrader.io/docs/latest/ and https://nautilustrader.io/docs/latest/concepts/execution/
- https://nautilustrader.io/docs/latest/concepts/execution/algorithms/ — native Rust TWAP; **verify API in pinned wheel** before binding.
- https://github.com/pola-rs/polars and https://duckdb.org/faq
- https://github.com/skfolio/skfolio and https://github.com/PyPortfolio/PyPortfolioOpt
- https://github.com/microsoft/qlib and https://github.com/optuna/optuna
- https://github.com/hummingbot-trading/hummingbot and https://github.com/QuantConnect/Lean
- https://github.com/polakowo/vectorbt/blob/master/LICENSE.md (Commons Clause caveat)

**Before selecting any dependency:** verify latest version against Python 3.13/3.14; check licenses/redistribution and transitive solver/GPL constraints; inspect CVEs/supply-chain, maintainer activity, test coverage, Windows and Arch installation, actual costs, API stability; benchmark against current locked stack. Do **not** add a package merely because it sounds sophisticated.

## 4. Institutional/public-method algorithm program (not a pile of indicators)

Algorithms below are **research candidates**, not claims of profit. Every candidate must identify its economic rationale, feed needed, valid asset horizon, execution requirements, estimated capacity, benchmark and validation procedure. Alpha is separable from portfolio sizing and execution.

### 4.1 Alpha/decision layer — proposals only until proven

1. **Diversified time-series momentum / trend**: volatility-scaled momentum on 15m/1h/4h/daily *closed* bars, with turnover penalties and regime awareness. Public trend-following concepts; benchmark plain moving-average and passive/market-neutral exposure. Avoid evaluating at 1m merely because 1m data exists.
2. **Cross-sectional momentum / residual momentum**: market/sector/coin-beta neutral factor portfolios, common-point-in-time instrument universe and shortability. Requires enough assets and realistic costs, otherwise downgrade to research only.
3. **Funding/basis and carry**: signed funding rates, mark/index, fees, borrowing, collateral and venue/region permission. Naïve annualized funding prediction is not a free arbitrage; future rates and liquidation risk are uncertain. No synthetic funding extrapolation permitted in qualification.
4. **Cointegration / statistical arbitrage**: rolling residual spreads and Kalman-filter/cointegration candidates; require stable cross-venue timestamp alignment, breaks/regime-drift stress, and borrow/short availability. A low p-value on formation data is not evidence after trading costs.
5. **Order book / microstructure signals**: imbalance, spread, realized volatility, quote update intensity and queue pressure, on future authentic tick/L2 data only. No OHLCV-derived pseudo-order book, no false latency edge.
6. **Market making**: publicly described inventory-penalized quote placement (e.g., Avellaneda–Stoikov) only after a real queue/partial-fill/maker-rebate/adverse-selection model and adequate venue demo evidence. Aggressive autonomous market-making is a **late-stage** task, not a first-run feature.
7. **ML/ensembles**: optional constrained factor models/gradient boosting through fully reproducible Qlib/other OSI tools. Use purged labels, timestamped features, calibration, permutation controls and ablation before allowing complex AI hypotheses to affect orders.

**Acceptance:** net-of-all-cost edge over baselines; out-of-sample uncertainty intervals; no data leakage; enough independent market regimes; capacity, drawdown/tail loss, strategy correlation and power across multiple historical captures; no self-approval from AI-generated explanations.

### 4.2 Portfolio construction — mature public methods

- **Ledoit–Wolf covariance shrinkage** (more stable than unregularized small-sample covariance); use point-in-time as-of inputs, evaluate turnover and CV error.
- **Hierarchical risk parity** already in `lab.allocation`: keep as an established benchmark, not automatic winner. Compare inverse-vol, risk parity and constrained mean–variance after fees and sample perturbation.
- **Constrained convex allocation** (e.g., CVaR/expected shortfall, gross/net/leverage/turnover/borrow/funding budgets): solve using free OSI numerical solvers, with explicit status/error fallbacks and allocations projected back through hard risk limits.
- **Black–Litterman** only when prior/forecast confidence is credibly calibrated; otherwise this can hide subjective returns behind mathematics.
- **Volatility targeting and drawdown-aware risk budgeting**: size as a forecast-error-aware budget. Fractional Kelly, if considered, requires robust independent return estimates and hard operator caps, not maximum growth at point estimates.
- Multi-strategy portfolio selection must be **approved as a whole**: a set of individually favorable strategies can combine into an unfavorable high-turnover/high-correlation policy.

### 4.3 Execution and venue microstructure

- Use **native Nautilus order state machines** and the upstream execution-algorithm interface; native TWAP exists in current docs, but pin and probe actual exports before registering.
- Compare market, marketable-limit, passive, TWAP, VWAP and participation-of-volume strategies **only if** the available feed has bid/ask, actual volume and venue acceptance semantics needed to simulate them.
- Public **Almgren–Chriss** style temporary/permanent impact models and square-root participation approximations are diagnostic constraints to be **calibrated to fills**, not free ex post slippage fixes.
- Order-book queue position, adverse selection, partial fills and cancel/replace race handling are mandatory before touting spread capture or high-frequency performance.
- D1's next-observed-trade simulator can prove Decimal position conservation and hard gates; it **cannot** rank live 1m execution algorithms. Source-captured spread/funding/slippage distributions must decide which strategies are deployable.
- A hypothetical taker fee of **5 basis points each side means 10 bps per round trip before spread/slippage/funding**, so 1m churn requires a genuinely unusually large *measured* after-fee edge. Prefer a 15m/1h/4h alpha horizon with 1m/tick monitoring by default.

### 4.4 Research/econometrics and independent challenge

The original system already has DSR, PSR/HAC adjustments, PBO/CPCV variants, stationary bootstrap, selection ledger and guarded holdout. Preserve these. Expand them only when:
- **Purging and embargo are based on actual label/event horizon**, not an arbitrary constant; overlapping cross-instrument signals are grouped consistently.
- **Multiple hypothesis trial counting** includes all model revisions, Optuna attempts, failures, AI proposals, and research modes. Compare with White's Reality Check / Hansen SPA as additional public benchmark tests when assumptions fit.
- **Nested train/validation/test** and one untouched prospective period prevent refitting after failures. Strategy/cost code, data manifests, account capabilities and selection rules are content-hashed.
- **Power and calibration studies** measure both type-I errors and plausible achievable power, including correlated signals, serial dependence, fat tails, outages and stale marks.
- **Fees, spreads, impact and funding** are independently timestamped inputs, not estimates from future information.
- **Performance reports** include downside/tail risk, rolling and conditional net alpha, per-side funding and turnover, fill-to-decision lag, reject/cancel rates, borrow/collateral, drawdown length and sample sensitivity.

## 5. Explicit architectural decisions

**AD-01 — One execution spine:** NautilusTrader events, strategy agents, risk and execution are authoritative. Standalone `lab.markets` contains *only* contracts, pure derivative math, bounded data conversion and a reference simulator. New full custom broker/OMS/execution engines are rejected.

**AD-02 — First-class evidence sources:** `MarketDataAdapter` and `ExecutionAdapter` must expose source/instrument/mode/account capability evidence and independent mark/index/quote/trade/funding timestamps. Unknown permissions, missing cost inputs or stale risk marks are fail-closed.

**AD-03 — Decision and execution separation:** strategy on 15m/1h/4h/daily by default, execution/risk continuously from tick/1m feeds. High-frequency alpha is an optional separately funded research program, not automatically superior.

**AD-04 — Safety orthogonal to aggressiveness:** presets choose an allocation/strategy budget *inside* immutable operator hard ceilings. No AI prompt, risk profile, adaptive model, or UI control may enable live routes or exceed accepted loss/margin bounds.

**AD-05 — Open-source only by default:** runtime requires no paid LLM, broker, cloud, vendor feed, or proprietary algorithm. Optional licensed data and exchange fees may still be necessary for reliable live-quality evidence. A strictly open-source distribution must exclude the Commons-Clause-restricted VectorBT code.

**AD-06 — Legal/open methods:** public, published mathematics can be independently implemented with attribution and appropriate license checking; never copy private investor trading rules or misrepresent a public formula as institutional alpha.

**AD-07 — Record everything:** every signal version, candidate, trial, feed manifest, regime state, order intent, fill, commission, funding, correction, override, cancellation and recovery has a durable provenance chain.

**AD-08 — Independent release:** each venue/product/mode combination has qualification evidence and a hard explicit release gate. Offline/no-credential first install precedes external demo; small-money live is a new independently approved subsystem, not a parameter flip.

**AD-09 — Natural-language instructions are untrusted drafts:** the planned "describe the desired trade" field produces only a **versioned, validated parameter proposal** with an explicit natural-language explanation. A deterministic parser/schema validator must map supported products, venues, account mode, position sizing, time horizon and risk objectives to legal operator bounds; ambiguous prompts abstain. The operator reviews a before/after diff, and activation requires explicit approval. The language model cannot mutate hard stops, credential scopes, research approval evidence, venue region, live authorization, or risk-engine constraints. Test prompt injection, adversarial sizing requests and unknowable market assumptions; no dependency on paid inference services for the core program.

## 6. Prioritized execution plan

| Priority | Work | Concrete gate |
|---|---|---|
| P0, now | D1 hardening and data/event correctness, reject illiquid fake fills, align Python CI history depth with collab job | Tests for edge cases; zero new trading regressions; full branch history fetched for cross-agent thread verification |
| P0, next | **Single data-event / account / instrument ontology in Nautilus** with pure Decimal derivative oracle, no duplicate agents | Offline crypto signal -> same Allocator/Risk/Execution agents -> replay account matches oracle snapshots, no future leakage |
| P0, next | Authentic OKX EEA demo capabilities and public quote/mark/funding/feed ingestion; use **read-only then supervised demo** | Verified eligible contract, region, order types, margin and funding; forced restart recons without duplicate order |
| P1 | Zero-credential first-boot experience in Windows and Arch dashboard/installer | Full clean-install-to-simulated-trade smoke in UI, no IBKR, optional external demo |
| P1 | Columnar Parquet catalog and performance/provenance suite with DuckDB/Polars research option | Reproducible multi-year/multi-instrument replay within measured CPU/RAM budget, dataset hash round-trips |
| P1 | Cost-aware alpha suite and robust optimizer references, behind independent experiment gate | Research trial lineage, statistically corrected selection, strategy/baseline net-of-cost and stress report |
| P2 | L2/exchange-calibrated maker, TWAP/VWAP/POV and microstructure algorithms | Recorded quote/book vs demo fills, impact and slippage calibration on each permitted order type |
| P2 | cTrader and equities paper alternatives, clean regional credentials and adapters | Authenticated demo lifecycles and reconciled positions, verified EEA eligibility and vendor licences |
| Future | Separate supervised small-notional real-money product | Production-qualified engine, weeks of demo evidence, independently reviewed security, monotonic loss caps, emergency cancel/flatten, explicit account authority and legal eligibility |

## 7. Falsifiable engineering and investment-quality metrics

- **Software**: state-reconstruction equality after crash; zero duplicate venue-submitted orders; no unknown-state exposure increase; 100% correct venue/product/mode guard tests; exact Decimal accounting identities; incident/recovery evidence; CPU/RAM/latency under measured workloads; passing clean-install tests across Arch and Windows.
- **Feeds**: deterministic source checksum and timestamp alignment; missing/duplicate/revision detection; mark/quote/trade/funding independence; no future-feature leakage; data eligibility masks and market calendar.
- **Execution**: adverse spread/slippage net PnL, actual fee/funding match to venue report, partial fill/cancel/replace/reservation parity, realistic queue/rate limits and latency distributions. No backtested maker rebate without maker fills.
- **Research**: explicit null hypotheses, clean trial count, controlled expected false discoveries, appropriate power, prospective untouched test, confidence bounds on *after-cost* advantage over a locked benchmark, calibration drift alarms, economic rationale and failure analysis. Never call a backtest a guaranteed return.
- **Operations**: blast-radius-limited API rights (no withdrawal keys), halt-on-stale-feed and stale-mark, no duplicate supervisors, tested backup/restore and power-loss recovery, audited release artifacts and dependencies.

## 8. Explicitly unresolved — not synonymous with software defects already fixed

1. **Python CI history-depth mismatch (G014 false failure)**: dedicated collab job checked out full history and passed while pytest's default shallow checkout could not resolve the remote ChatGPT opening message for G014. The Python CI job now requests `fetch-depth: 0`; verify both matrices on the next completed run. No immutable thread message was edited.
2. **Crypto demo adapter acceptance and regional permissions**: account credentials, EEA X-Perps product mode, demo API order lifecycle and pinned release adapter semantics not exercised. The existence of `OKXEnvironment.DEMO` does not qualify them.
3. **Journal replay and OMS/agent recovery for crypto**: simulator correctly refuses a nonempty journal; complete deterministic reconstruction pending.
4. **Crypto tick/quote/mark/index/funding source fidelity**: read-only candle endpoint lacks required contract and quote events. Algorithms needing L2 cannot be qualified.
5. **Installer and GUI mode architecture**: existing app still boots into US-equity IBKR setup, not offline crypto on first run.
6. **Statistical release audits**: `docs/release-criteria.json` includes pending broker, research calibration, independent security and operations tests. No blanket "all bugs resolved" conclusion is warranted.
7. **Quant performance evidence**: no prospective data proves any strategy's durable after-cost edge. Open-source platform quality is necessary but insufficient.
8. **Venue-specific legal eligibility / live account authorizations**: independent checks and human approval required; no order-routing fallback allowed.

**Review request:** check the fixes and P0/P1 choices with Claude; no push/merge to `main` without explicit owner authorization. Do not turn this audit into another full parallel trading framework.
