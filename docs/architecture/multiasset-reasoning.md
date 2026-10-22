# Multi-asset and evidence-driven reasoning — implementation and qualification

**Current implementation:** `src/lab/multiasset.py`, `src/lab/multiasset_report.py`, `src/lab/stress_reasoning.py`; all are **offline, read-only, research-only**. Their output cannot authorize an order or modify a brokerage account. Forex, equities, ETFs and plain cash bonds are supported as typed research instruments; this is **not** a claim that the NautilusTrader broker adapter can trade any of them safely.

## 1. Capital-market instrument distinctions

| Asset / market | Research contract in this slice | Before any broker adapter qualifies |
| --- | --- | --- |
| Equity / ETF | Permanent instrument ID, venue/currency metadata in downstream future registry, unit/lot size, bid/ask, corporate-action-aware positions | Dividends, splits, halts, concentration, market calendars, trading permissions, point-in-time universe |
| FX spot | **Two currency cash legs**, base and quote currencies, fresh timestamped bid/ask and amount rounding | Pair-specific venue, settlement/holiday conventions, rollover/interest, weekend/24×5 session logic, spread depth, financing, counterparty and margin permissions |
| Cash bonds | Face-value quantity, quoted clean bid/ask per 100 par plus separately supplied accrued interest | ISIN/venue/currency, full coupon schedule, curve, credit and liquidity risk, convexity, day-count, settlement and defaults |
| Options / futures / CFDs | **NOT SUPPORTED**; no dummy generic equity adapter | Complete derivatives spec, contract multipliers, option Greeks/nonlinearity, variation/initial margin, liquidation, venue/broker qualifications |

Forex is neither an equity that trades on the NYSE closing session nor a generic stock with a different ticker. Spot purchases increase base-currency holdings and consume quote-currency cash. The default research policy **prohibits borrowing and negative settled cash**. Leveraged FX is not silently approximated by cash-market trades.

## 2. Type safety and valuation

Cash, price, quantity, notional and conversion use `Decimal` rather than binary floating-point for accounting-like calculations. The instrument contract specifies lot step, minimum amount and tick size. A timestamped quote has explicit bid/ask, currency direction, source and maximum staleness; crossed markets or inconsistent timestamps are rejected.

A positive source-currency cash asset is liquidated via the bid for base→quote (or inverse ask). Negative exposures, where used in conversion estimates, are marked at the conservative replacement side. Arbitrary triangular FX graph cycles are **rejected** because they could produce contradictory routes; allowed quote graphs have exactly one path per pair, with a small maximum hop count. These indicative marks do not prove executable liquidity or settlement at those prices.

Equity/ETF positions mark at bid. Plain bond holdings use quantity × (clean bid + accrued interest)/100. Short securities, negative cash, leverage, inconsistent FX holdings and missing price evidence are rejected. Values are **liquidation proxies** excluding market impact, taxes and omitted fees.

## 3. Complex but auditable scenario reasoning

The stress engine is an explicit factor-exposure graph:

- A **factor** is a named hypothetical shock, e.g. `rate_parallel=+0.01`, `risk_off=-0.25`, or `usd_strength=+0.05`. Factors are **not** predicted probabilities.
- A **sensitivity** attaches that factor to a specific security or FX rate with a decimal loading and a mandatory source, e.g. bond return sensitivity `-5` to a +1% rate shock yields a -5% price scenario.
- Several factors can combine additively into one declared asset price shock. Equity/bond marks and FX bid/ask curves are repriced; bond shocks affect the clean price, while accrued interest remains separately declared.
- A direct price shock and an active factor-derived shock for the same target are **contradictory evidence** and terminate evaluation. Unknown factors, duplicate scenario names, impossible bounded shocks and missing price feeds also fail closed.
- Each scenario produces a complete bid-side portfolio mark, total P&L, cash/security-level P&L attribution, traceable factor contributions, largest losing component, and breaches of user-declared absolute/fractional **stress-test limits**.
- Scenarios are ranked by worst modeled loss. Component attribution is checked against reported P&L to a strict Decimal computational-rounding tolerance of 1e-12 reporting-currency units; the residual is explicitly reported. Larger unexplained differences terminate evaluation.

This is deterministic *counterfactual analysis*, not an autonomous investment recommender, a trained causal graph, a calibrated probability distribution or a validated risk forecast. A scenario limit non-breach is **not** a claim of portfolio safety; merely the declared hypothetical events were evaluated without crossing those declared limits.

## 4. Reproducible CLI workflow and UX

```bash
lab capital-audit --manifest examples/multiasset-offline/portfolio.json
lab capital-audit --manifest examples/multiasset-offline/portfolio.json --json
lab capital-audit --manifest examples/multiasset-offline/portfolio.json --output results/capital-audit.json
```

The manifest version is 1. It has: `as_of` (UTC), `max_age_seconds`, `reporting_currency`, `instruments`, `prices`, `fx_quotes`, `cash`, `holdings`, optional `spot_previews`, and optional `factor_exposures`, `scenarios`, `stress_policy`. All decimals are **strings**; quotes require sources; stress exposures require assumption provenance. The sample file models US shares, a USD-denominated bond, EUR/USD cash conversion, a proposed spot purchase and combined rate/equity/currency stress.

The report contains separate **before** and **after preview** risk analyses, with JSON SHA-256 evidence identifiers and qualitative findings. It uses exclusive file creation to prevent overwriting an earlier experiment. A missing scenario set produces `NO_SCENARIO_EVIDENCE`, never a green safety verdict. The planned web experience should present scenario waterfalls, ranked driver charts, evidence sources, uncertainty warnings, and conflicting-hypothesis alerts before displaying a hypothetical return. Avoid UI elements that imply an exploratory limit check permits an order.

## 5. What "high reasoning" means here

The platform should support a future **bounded research deliberation loop** (the AI research swarm currently exists, but is not this complete system):

1. **Form competing hypotheses** and causal mechanisms with explicit assumptions and prior evidence; register all generated attempts.
2. **Retrieve and verify evidence** with exact data revisions, timestamps, venue calendars, corporate-action metadata and model provenance; identify unsupported claims.
3. **Model causal alternatives** using factor exposure graphs, stress pathways, lag/lead mechanisms, cost/liquidity constraints and model uncertainty. Do not equate correlations with identified causal effects.
4. **Cross-examine** through a separate Critic and adversarial scenario generator. Record counterexamples and unresolved contradictions.
5. **Quantitatively test** with leakage-free rolling/nested validation, calibrated uncertainty, out-of-sample benchmarks and multiple-testing correction.
6. **Abstain** on invalid data, unsupported events, model drift, impossible accounting or evidence disagreement; log what additional data would resolve it.
7. **Publish** a reproducible research dossier and a reviewable risk trace, not executable orders or an automatic strategy promotion.

Use LLMs for hypothesis formation, report synthesis and critique **inside isolated research workers**; use deterministic typed validators for accounting, experiment gates, capital limits, and broker safety. More reasoning tokens or larger models do not supply ground truth. High-end reasoning must be *testable* against explicit outcomes and failure injection.

## 6. Implementation and deployment gates

- Offline deterministic tests check FX quote direction/spread, bid/ask staleness, FX cycles, spot cash legs, bond clean/dirty prices, precision, hypothetical portfolio cash funding, conflicting stress paths, mathematical P&L attribution and reproducible fingerprints.
- CI executes the sample `capital-audit` CLI in both supported Python versions. It cannot validate live liquidity, broker settlement or capital requirements.
- **Paper order execution remains US-equity-only at this stage**, and is itself still awaiting complete broker reconciliation and crash-safety qualification. No Forex or bond order adapter is enabled.
- Follow-on priorities: market-specific data ingestion and calendars; instrument master/corporate-action histories; spot FX settlement and rollover; fixed-income cashflow/duration/convexity; robust portfolio optimization and calibrated multifactor uncertainty; IBKR paper integration tests by asset and separate per-asset risk permits.

Do not treat a complex stress trace as a definitive capital forecast. It is an explicit, falsifiable analysis of supplied assumptions.
