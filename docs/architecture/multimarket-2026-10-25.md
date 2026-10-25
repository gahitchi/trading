# Trading Lab 2 — multi-market architecture decision record

**Status:** REVISED AFTER TECHNICAL REVIEW — incremental implementation only, no authorization to trade money.  
**Date:** 2026-10-25 · **Owner:** Gabriel · **Author:** ChatGPT · **Base:** main at e65502961b5957c56821644cf04df7b7787b2d00.  
**Scope:** crypto first (24/7), then forex (24/5), then listed securities; free/open-source tools preferred; installed program usable without a broker login; externally hosted demo before small-capital live, weeks later.

## 0. Interpretation and invariants

* This is **not** a rewrite of the mature US-equity research stack. Keep NautilusTrader, existing agents, research isolation, holdout, operator mandates, IBKR hard stops, replay harness and journal. Build a separate, typed, additive multi-market domain; move clients to it only behind parity tests.
* **No implicit live-money enablement.** The original IBKR executor stays paper-only; proposed interfaces must not offer production routing. A distinct, independently reviewed live product and operator permission are necessary in the future. A setting, environment variable, dashboard toggle or strategy output may **never** remove this protection.
* **Execution venue != asset class != operating mode.** The operator chooses a source of market data, an instrument, a provider account, and a mode independently, subject to actual capabilities. Every unsupported combination must fail closed, never silently substitute a different instrument or environment.
* Crypto trading is 24/7 **market availability**, not a mandate to generate trades 24/7. Absence of credible edge / unhealthy market data produces abstention. Forex weekends/holidays and equities exchange sessions require venue calendars.
* "Aggressive" changes *allowed exposure within hard operator limits* and eligible strategies, never statistical evidence requirements or emergency limits. Risk must not be bypassed by natural-language trade settings.

## 1. Existing state and migration surface

The current code is a **US-stock IBKR paper** implementation:
* `src/lab/config.py` has Reg-T stock margin, IB host/client, XNYS instruments and US-stock calendar; `src/lab/exchange.py` is XNYS only.
* `src/lab/intraday.py` accepts daily/1h/15m/5m, and bars are NYSE regular-session bars; adding a "1m" enum to it without changing session, data, and execution semantics would be wrong.
* `src/lab/paper.py` directly constructs Interactive Brokers Nautilus factories. `src/lab/app.py` hardcodes DU-prefixed IBKR setup. `src/lab/safety.py` correctly rejects live ports and accounts. Keep all of them unchanged until the new market-level tests pass.
* `src/lab/sim/broker.py` implements an IBKR-specific execution harness, not general crypto accounting.
* `src/lab/agents/messages.py` uses ticker strings and equity/stock quantity semantics; typed instrument IDs and contract multipliers are needed at an adapter boundary rather than reusing stock assumptions.
* Several quantitative and release findings remain open in `docs/release-criteria.json` and `docs/audits/2026-10-24-quantitative-research-upgrade-audit.md`; this new work does not close them.

**Migration strategy:** "strangler" architecture: add `lab.markets` solely as a narrow typed domain, pure accounting oracle and data-conversion boundary, validate it offline, then integrate its **same** accounting functions into Nautilus market-data and execution agents. Never duplicate strategy, allocator or risk-policy agents. Do not retarget the existing equity paper strategy by config aliasing. Existing `lab paper` must remain exactly as restrictive as it is today.

## 2. Layered system

```text
UI / CLI (local)                          Operator approval & audit log
        |                                               |
Control service (capabilities, modes, identities, runtime locks, permissions)
        |
Research (fixed hypotheses, train/test/holdout, statistical gates, sandboxed AI)
        |                     Market data ingest (immutable raw + normalized UTC)
        v                          |
Strategy signals -> portfolio allocation -> independent risk engine
                                                   |
                                  durable order-intent journal / outbox
                                                   |
                 +---------------------------------+------------------------+
                 |                                 |                        |
             internal SIM                     external DEMO          LIVE (NOT WIRED)
             deterministic fills              approved venue          separate future
             instrument rules                 credentials            reviewed subsystem
                 |                                 |
          event ledger                        reconciliation
                 +--------------- reports & provenance --------------+
```

**Single source of truth:** event ledger with immutable order intents, observations, exchange fills, fees, marks, funding, corrections and reconciliations. Use unique event keys and transactional persistence; idempotent replay after a crash. An order acknowledgement is **not** a fill. Venue order ID != client order ID. Unknown order status freezes new exposure until reconciled. Cross-process / cross-machine writer lease required.

**Domain boundaries:**
1. `Instrument`: stable `venue:instrument_id`, asset class, spot/linear/inverse contract kind, base/quote/settlement currencies, multiplier, tick, quantity step, minimum qty/notional, margin eligibility, region, trading status, funding interval.
2. `MarketEvent`: exchange timestamp and *separate* receive timestamp; source, sequence, bid/ask, last, trade quantity, mark/index, funding rate, normalized UTC ns. Never use received-future bars as knowledge at decision time.
3. `OrderIntent`: client-generated idempotency key, account fingerprint, strategy & decision identity, instrument, side, quantity, order type/TIF, optional limit/stop, reduce-only flag, expiry, max slippage. Explicit model of creation -> risk approval -> persisted outbox -> submitted -> pending -> partial/fill/cancel/reject/unknown.
4. `AccountSnapshot`: cash by currency, collateral, open signed positions by contract, realized/unrealized PnL, fees, available margin, venue maintenance requirements, pending orders and liquidation warnings.
5. `VenueCapabilities`: discover permissions per account **and instrument** including shortability, allowed order types, precision, instrument catalogue, market-data granularity, demo availability and country/product eligibility. Unknown => deny.
6. `ExecutionAdapter`: contract to query capabilities, snapshot/positions, submit/cancel, reconcile fills/orders; `MarketDataAdapter`: subscribe/read only; credentials with no withdrawal rights. Pure SIM has a separate in-process implementation.

No adapter returns `supported=True` merely because a broker advertises the product in another jurisdiction. EEA must be evaluated independently; OKX X-Perps are not identical to global perpetual swaps. Eligibility, contract IDs and margin treatment are verified at runtime from the authenticated EEA account.

## 3. Reference simulator (mandatory first product)

A usable **offline, zero-credential** account with explicit simulated starting balance and no real broker dependencies. This is our first deliverable and regression oracle, *not* evidence of live profitability.

* Small pure Decimal accounting source (`lab.markets.accounting`) shared by simulation and future Nautilus bridge; no independent portfolio/agent subsystem. Linear contracts only in first build. Do **not** model inverse futures using stock share equations. No free capital from opening a short; margin is reserved, PnL and fees accounted against equity.
* Explicit weighted-average position transition and **signed, bidirectional** funding cashflow at the exact venue settlement timestamp. `payment = signed_position × mark × multiplier × funding_rate`, with a positive result debiting collateral. Positive rate charges longs and credits shorts; negative reverses this. Unknown funding -> do not pretend validated economics; refuse futures-performance qualification without funding history.
* Fee model with maker/taker distinction and venue fee tier, minimum tick/quantity/contract size. Conservative slippage and marketable-order rules; no passive fill solely because the bar touched the limit. Include order rejection, partial fills, disconnection and restart injection.
* **Dual-price accounting is mandatory:** independent `MarkPrice` events determine unrealized PnL, equity, initial/maintenance risk and funding; observed trades/bid-ask determine execution (with explicitly adverse assumptions). A last-trade wick alone cannot trip mark-based liquidation or drawdown. If marks are missing/stale, reject increases; never fabricate a mark from last price for market-validated runs. Equity = settled collateral + realized PnL + open mark-to-market PnL - charges. Stale/invalid/nonpositive marks block risk-increasing orders.
* Streaming minute bars derived only from timestamped trades, aligned to `floor(UTC second / 60) * 60`, emitted after the window closes under a configured watermark; report gaps and late data; never synthesize OHLCV for minutes with no trades. Historical years of crypto minute research use `lab.markets.minute_batch.aggregate_trades` columnar NumPy reductions (later Arrow/Parquet catalogs), **not** one Python event object per candle. Float arrays for feature extraction; exact Decimal for accounting. For low-latency exits, process quotes/ticks, not only closed candles.
* Separate `spot`, `linear_swap`, `dated_future`, `forex`, `equity` instruments. Spot shorts disallowed unless authenticated margin borrowing supports them. Simulation defaults to explicitly labelled `linear_swap` assumptions for long/short demonstration.
* Replay two independent paths, using the SAME decisions and cost data: strategy-level replay and broker-event replay. Compare fills, equity and exposure to tolerance; never claim market replay parity from simplistic OHLC fills.

## 4. Broker rollout

**Candidate 1: OKX EEA**, because its official EEA v5 API documents `x-simulated-trading: 1` REST demo and distinct `wseeapap` demo WebSocket endpoints, and NautilusTrader's OKX adapter documents `OKXRegion.EEA` and `OKXEnvironment.DEMO`. Explicit qualification tasks:
- **D0 probe completed** in pinned `nautilus_trader==2.0.0rc7`: `OKXRegion.EEA`, `OKXEnvironment.DEMO`, `OKXDataClientConfig` exist; *the guessed name* `OKXExecClientConfig` does **not** exist. IBKR adapter import succeeds; `nautilus_trader.adapters.alpaca` and `.ctrader` are not importable in this pinned wheel. This proves exports only, **not** EEA X-Perps order-mode compatibility. CI runs `python -m lab.markets.capability_probe` before tests; inspect correct execution factory/config class names and demo API behavior before D2 integration.
- Verify demo API credentials in EEA, eligible demo X-Perps instrument IDs, venue min notional, position mode, reduce-only and funding/settlement. Verify zero withdrawal permission and correct regional hostname **before any order**.
- First read-only public feed, then account/positions read-only, then supervised demo round trip; reconcile all fills, fees, funding and cancellations after forced restart.
- No global production API key accepted by demo code. No automatic fallback from unavailable EEA demo to global or live. If unsupported, choose another externally hosted demo after structured evaluation.

**Candidates 2-3:** forex cTrader demo (24/5, country/product/account restrictions) and equities Alpaca paper (market permissions, IEX-only free coverage and shortability). Retain existing IBKR paper as optional equities venue. Future live candidates require fresh separate product approvals. Venue-specific adapters should wrap supported upstream open-source Nautilus implementations, not fork vendor implementations without cause.

## 5. Mode and authorization policy

| Mode | Credentials | Order destination | Purpose |
|---|---|---|---|
| OFFLINE_SIM | none | isolated simulator | first-run demo, unit tests, replay |
| BROKER_DEMO | demo-only read/trade key | explicit approved regional demo API | realistic exchange lifecycle |
| LIVE | distinct future capability, **not implemented** | no existing path | eventual small-capital activation |

Each runtime identity binds mode, venue, account fingerprint, instrument universe, strategy SHA, data source, costs, risk config and build SHA. Storage and credentials segregated by mode; never copy a demo authority/ledger into live. UI must display mode persistently, offer emergency cancel/flatten with explicit uncertainty, and require human action to start trading. An LLM/natural-language setting parser only outputs an untrusted *draft*; static validators and risk limits remain authoritative.

## 6. Risk architecture

Order gate AND continuous exposure monitor (independent of strategy):
* account-level max collateral allocation, order notional, per-instrument exposure, net/gross, maximum effective leverage, short exposure, order rate, cumulative turnover, drawdown, daily/rolling loss, concurrent pending exposure, slippage/spread.
* initial/maintenance margin **by instrument** using venue schedule; liquidation price estimates are warnings, not guarantees. Isolated vs cross modeled explicitly. Trading fees, funding, tiers, conversion and borrow costs are not optional for qualification.
* venue outage, stale/out-of-order data, inconsistent exchange equity, unknown order state, clock skew, irreconcilable ledger, insufficient collateral, unexpected account/product mode => block increases and hard halt; reduce-only paths have separate audit/permission. Attempt server-side protective orders when supported, but do not assume they survive every outage.
* Hard ceilings configured by operator cannot be raised by a risk profile, LLM, or automated policy. No self-recovery from hard halts without explicit reviewed recovery. Tests for simultaneous opposite fills, cancel/fill races, partial fills, margin change, cross-position exposure and restart.

**Initial small-capital mode:** trade sizes may fall below venue minima; reject honestly instead of resizing to unsafe nominal. A small account cannot promise meaningful one-minute profit after fees.

## 7. Data and research discipline

* Raw immutable feed with source, timestamps, region, instrument ID, vendor revisions, trade/quote sequence and gaps; derived Parquet partitions by `venue/type/instrument/day`, with dataset manifest+checksums.
* 24/7 calendar for crypto, explicit DST-aware sessions for forex, actual exchange calendars for equities.
* No paid feed required to install, but free public feeds are not automatically qualified for trading. Distinguish last-trade, best bid/ask, order book, index and liquidation mark. Refuse backtests if required data or point-in-time parameters missing.
* Purged, embargoed walk-forward and realistic costs; independent untouched holdout, seeded replay, statistical selection-accounting as in original system. The R01-R19 audit remains a separate blocker until verified fixes land.
* Report gross and net returns, hit rate, alpha baseline, tail loss, turnover, fees, funding, drawdown, exposure, liquidation proximity, fill quality, missing data, and stability over regimes. Comparing highly tuned strategies to simplistic buy-and-hold is not sufficient for derivative claims.

## 8. UI/UX and installation

First boot: `Choose Offline Simulation` (default, no credentials) -> simulated capital & quote currency -> optional 24/7 crypto 1-minute historical replay -> strategy/preset -> inspect final allowed risk -> press Start. The existing IBKR installer must remain valid during migration but be made optional in a later separately tested UI step. No Docker group escalation without explicit informed choice.

Subsequent boot: account and operating mode clearly labeled; instrument selector filtered by verified venue capabilities, strategy/preset, capital allocation, risk controls, health badge, detailed orders/positions/fees and one-click halt. Use a guided connector for demos, never ask for raw keys in URLs and never log secrets.

Cross-platform Arch and Windows. Offline SIM usable without GPU, a paid LLM, subscription, exchange login or cloud account. LLM research remains optional. VPS deployment only after recovery/soak testing.

## 9. Phased implementation & exact completion gates

**D0. Documentation and pinned-dependency probe:** this ADR, transition boundaries, locally verified 2.0.0rc7 import surface and runnable skeleton. Gate: design reconciled with existing invariants and known open audits. Probe evidence above and in GitHub Actions test output.

**D1. Shared accounting oracle + minimal offline reference:** typed contract/capabilities/order events, vectorized historical and streaming 1-minute UTC builders, shared `lab.markets.accounting` linear short/long margin/PnL/funding math, explicit independent mark prices, deterministic approximate trade execution, file/CSV replay, CLI, tests. **No new strategy/allocator/risk agent stack.** Gate: passing local unit/property scenarios on finite/duplicate/late timestamps, margin, short, reversal, fees, funding, crash behavior; no network orders. Note: a tiny reference simulator is *not yet* a broker-grade realistic matching engine.

**D2. Nautilus bridge & market data:** register typed crypto instruments in Nautilus, a verified EEA public trade/mark/bid-ask feed, replay-catalog adapters and data manifests. Existing AllocatorAgent/RiskAgent/ExecutionAgent policies apply with venue-aware instrument/margin bridges calling the **same** pure accounting oracle; no parallel agent framework. Gate: identical sequence/snapshot semantics and no look-ahead.

**D3. External demo:** verify real pinned Nautilus OKX adapter, supported region, demo account type, encrypted/OS-protected credentials and actual order lifecycle on OKX EEA demo. Gate: restart, partial fills, out-of-order updates, reconciliation, HMAC signing validation, min-size/precision, support for order and stop types tested against venue. No live API accepted.

**D4. Packaging:** integrate new account selection in both installers/dashboard; offline default. Gate: fresh installs, upgrade and rollback, startup with IBKR absent, supported Windows/Arch, secrets isolation.

**D5. Multi-market:** cTrader forex and Alpaca/IBKR equities through adapters and independent market calendars. Gate: end-to-end paper lifecycle per venue, no stale borrow inventory, correctly translated account fees/margin, cross-account risk.

**D6. LIVE RELEASE (future, separately authorized):** upgrade engine to qualified production release, independent security and market-data review, multiple weeks forward demo evidence, conservative small-capital sizing, human consent and new transaction-signed live entitlement, staged canary, hard monetary loss caps, venue API keys with no withdrawal permissions, independent watchdog, tested rollback, incident handling, account/tax/legal obligations. **Not authorized by this document, and not implemented during D0-D5.**

## 10. Architecture tests and threat model

Safety invariants to test: **(I1)** no live route or live credentials anywhere in D1-D5; **(I2)** no unresolved submission replay; **(I3)** no notional/position/order over hard caps; **(I4)** no synthetic/forward-looking completed bar; **(I5)** no unqualified instrument/account combination; **(I6)** no free short-sale cash or unmodelled funding; **(I7)** no deleted immutable audit event; **(I8)** mode-specific lock and account state survive restarts.

Adversaries/failures: misleading AI source, incorrect LLM settings, malicious API input, exchange reorg/correction, clock skew, stale public feed, connectivity loss, incorrect product ID, partial cancellation, API key exfiltration, bad upgrade, volume exhaustion, disk-full, duplicate processes, venue policy change and human misconfiguration. Protect each with static validation, separate process permissions, live disabled, event journaling and fail-closed risk.

## 11. Source constraints

Verified 2026-10-25 against:
- OKX EEA API `https://my.okx.com/docs-v5/` — demo host separation / x-simulated-trading header.
- NautilusTrader `https://nautilustrader.io/docs/latest/integrations/okx/` — `OKXRegion.EEA`, `OKXEnvironment.DEMO`. **Compatibility with pinned rc7 not yet proven**.
- OKX EEA X-Perps `https://www.okx.com/en-eu/learn/what-are-x-perps-guide-for-european-traders` — eligible EEA demo and product-specific access.
- `README.md`, `COLLABORATION.md`, `docs/release-criteria.json`, `docs/audits/2026-10-24-quantitative-research-upgrade-audit.md` on main e6550296.

## 12. Implementation boundary for this coding turn

Implement D1 additive, strictly local and paper/sim only; no network-capable production order connector; do not change existing IBKR protections or main. File system and tests must be usable via an explicit `python -m lab.markets` entrypoint once project dependencies are installed. All other phases remain concrete, **unimplemented** future gates; success of D1 does not qualify D3 or D6.

## 13. Review amendment — 2026-10-25 (C001-F13 recurrence prevention)

The D1/D2 split is a **test boundary, not an architectural division**. The execution engine remains NautilusTrader and the original agents. The `lab.markets` package is permitted only for small reusable: (a) Decimal instrument accounting, (b) normalized typed interfaces, (c) market-data ingestion/columnar transforms, (d) an offline reference fill harness, (e) journal and verification utilities. Do **not** recreate a strategy evaluator, portfolio allocator, autonomous risk-management service or long-running parallel execution stack under `lab.markets`. In D2 the stock and crypto workflows will share existing actor infrastructure with market-appropriate instrument/ledger adapters; no separate production-grade simulation engine should grow here.

**Pinned wheel probe evidence (GitHub Actions on 2026-10-25, Python 3.13, NautilusTrader 2.0.0rc7):**

| Adapter | Observed in pinned environment | Next action |
|---|---|---|
| OKX | Module loads; `OKXRegion.EEA`, `OKXEnvironment.DEMO` and `OKXDataClientConfig` present | Resolve actual execution-client factory/config names; independently verify regional X-Perps demo order support |
| IBKR | Module loads; known data/execution config names present | Preserve existing paper-only use |
| Alpaca | `nautilus_trader.adapters.alpaca` not importable | Qualify a supported external adapter or dependency when equities expansion starts |
| cTrader | `nautilus_trader.adapters.ctrader` not importable | Qualify a compatible adapter independently when forex expansion starts |

**Cost realism:** a hypothetical 5 bps taker fee each way creates 10 bps round-trip costs *before* spread/slippage/funding. Do not assume a fixed BTC/ETH minute volatility number: estimate empirical volatility and cost distributions for the specific venue, instrument, fee tier, size and date, and abstain when expected net edge is insufficient. The alpha/decision clock defaults to 1h; 1m is an execution/monitoring clock, not an instruction to open a new position every minute. Protect exits at tick/quote resolution where available.

**Throughput:** a 365-day crypto year contains 525,600 minutes *per instrument*. Raw message streams can be far larger; use NumPy columnar historical reductions plus planned Arrow/Parquet manifests and batch/group operators. Streaming `MinuteBuilder` handles bounded real-time event state, not millions of historical Python `Trade` allocations. Batch research does not use floating-point money for PnL.

**Valuation versus execution:** the v1 reference takes explicit `MarkPrice` and `Trade` streams; old example CSV must supply `mark_price` as an independent field. A synthetic CSV is *not* an exchange-validated mark source. Signed funding must be tested for both longs and shorts with both positive and negative rates, settled at explicit monotonic funding timestamps. Stale marks fail closed on risk increases. A last-price flash wick with healthy independent mark is not a maintenance breach. Future engine integration needs venue-specific mark/index contracts, funding schedules, collateral conversion and separate best-bid/ask for execution.

**Provenance and legacy approvals:** `simulation_digest(market="equity")` intentionally **excludes `lab/markets/**`** so subsequent crypto-only changes do not change equity research implementation identity; `simulation_digest(market="crypto")` includes it. `runtime_digest()` continues to cover every module. Note the unavoidable migration caveat: modifying `provenance.py` itself changes the old calculated SHA at least once. Do not represent this as zero-impact on previously signed approvals; invalidate/requalify legacy approvals as required unless a separately audited migration reconstructs the old exact implementation identity. Changing shared agents/accounting later also invalidates affected approval contexts. `deployment_context()` currently defaults to equity; a proper crypto evidence namespace and schema will be introduced before D2.

**New binding test gates:** (1) no new duplicate actor stack, (2) four-way signed funding matrix, (3) flash last-price wick cannot trigger mark-based halt while a mark move can, (4) offline entry requires non-stale independent mark, (5) historical 525,600-minute vectorized aggregation without fabricated bars, (6) probe pinned exports in CI and fail regional integration if unsupported, (7) crypto file edits do not change current equity simulation digest, and (8) *existing* IBKR live-account prohibitions remain intact. These are **necessary but not sufficient** for exchange demo approval.

**Current status:** D1 is a proof-of-concept. Journal recovery, mark-feed verification, effective order-book fills, vectorized long-history source provenance, EEA demo execution and real-money order routing are not qualified. D3-D6 remain blocked.

## 14. D1 acceptance checkpoints — 2026-10-25

Three review checkpoints are **binding regression gates**. The D0/D1 architecture is approved for further implementation, **not** approved for demo or real-money exchange execution.

**A. Linear position transitions.** `lab.markets.accounting.linear_transition` is the pure shared oracle. Same-direction adds produce a quantity-weighted entry with zero realized PnL; partial reductions realize only the closed quantity while keeping surviving entry unchanged; complete reductions set position/entry to zero; a sign-flipping fill atomically realizes the entire previous position and anchors the residual reversed book at the fill price. `tests/test_markets_accounting_transitions.py` covers long/short adds, profitable/loss-making reductions, full closes, reversals, non-unit contract multipliers, fee calculation on the *entire* reversal fill, and mark-at-fill PnL conservation. **The existing production agents have not yet been migrated to call this oracle**: that is a D2 integration condition and must pass equivalence tests first.

**B. Equity provenance isolation.** The existing method is named `simulation_digest()`, **not** `source_digest()`; it is called from `research/approval.py:deployment_context`. Default/explicit equity digests exclude the new `markets/` namespace while including changes to shared research/execution files. `tests/test_markets_provenance_isolation.py` checks that changing a crypto-only source changes crypto + whole-runtime hashes but **does not** change the existing equity simulation hash; changing shared risk implementation must invalidate equity. This is *scope isolation*, not grandfathering: the original one-time `provenance.py` change does change historical SHA identities, as recorded in Section 13. Never force acceptance of mismatching pre-existing approvals.

**C. Real adapter exports and D5 compatibility.** CI on `nautilus_trader==2.0.0rc7`, Python 3.13/3.14, confirms these actual public OKX client symbols:
`OKXDataClientConfig`, `OKXDataClientFactory`, `OKXExecutionClientConfig`, `OKXExecutionClientFactory`; `OKXRegion.EEA`, `OKXEnvironment.DEMO` exist. The guessed `OKXExecClientConfig` is absent. The frozen wheel imports its IBKR adapter; it does **not** import modules `nautilus_trader.adapters.alpaca` or `nautilus_trader.adapters.ctrader`. D5 must therefore either (1) adopt a separately evaluated, pinned Nautilus distribution/version that actually ships qualified integrations, **or** (2) build thin, testable, provider-native Alpaca v2 / cTrader Open API wrappers implementing the same `MarketDataAdapter` and `ExecutionAdapter` contracts, with account-region eligibility, credential scopes, cancel/reconcile and independent paper acceptance tests. Absence of an import today does not establish that a future version includes it. **Do not delay D1/D3 crypto work** on this decision, but do not promise D5 adapter parity.

**D1 verification and gate discipline.** New test coverage is added on this dedicated `chatgpt/multimarket-architecture-20261025` branch. Successful pure-accounting tests are not evidence of profitable alpha, realistic maker fill models, external paper acceptance, restart recovery, or live-money readiness. An inherited malformed collaboration-history thread `G014` currently fails `tests/test_collab.py` even when new D1 tests pass; this should be fixed by the responsible thread owner without mutating earlier records in this branch. `D1-offline-simulation` is still **partial** until the remaining release-gate conditions are fulfilled.
