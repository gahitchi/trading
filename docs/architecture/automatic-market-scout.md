# Default market scout: zero-symbol startup (design + first research-only implementation)

**Status (2026-10-25):** research-only shortlist builder is implemented in `lab.discovery`. The end-to-end market-wide feed, strategy validation and autonomous execution are **not** implemented. No code in this module can submit an order.

## Product behavior

When the user gives an explicit stock watchlist, only those stocks may enter that run's research/execution universe. If one is missing or ineligible, report the rejection rather than silently substituting another. When **no stocks are specified**, automatically enter `automatic_market_scout` mode. Fetch an eligibility-qualified *as-of* US equity universe from the configured authorized market provider, not an arbitrary hardcoded ticker list. Rank and select a bounded, diversified research shortlist with documented reasons and exclusions. **Zero qualified candidates = hold cash/abstain**, not a crash or a forced trade. All candidates must still pass research gates, strategy selection, up-to-date quotes, liquidity, account capability, reservation risk and operator authorization before any simulated or broker order.

This should be a core first-run flow, not an experimental command hidden from the user. The eventual dashboard must show **what was scanned, why each instrument passed or failed, evidence ages, source links, insider filings, cost estimates and the resulting abstain/trade decision**. It must default to dry-run research when no execution authorization exists. The risk profile changes risk budgets, not evidence standards.

## High-integrity evidence sources

1. **Exchange/broker/provider-qualified listing universe:** include actual venue and instrument permissions, active trading status, point-in-time membership (including survivorship and delistings), IPO date, legal access and corporate actions. Missing membership or provenance => deny. Daily and intraday market observations must record BOTH exchange and local receipt times. Do not impute missing stock history or use a future membership file in historical tests.
2. **Auditable price and fundamentals layer:** market cap, price, split/dividend-adjustment basis, turnover, dollar ADV, spread, volatility, sector/industry, statements and as-filed availability dates. Fundamentals must be visible only from their actual public release time, not the period-end date. The initial `EquityObservation` implements only coarse liquidity/market-cap/time gates; no value or alpha score exists yet.
3. **Official US insider filings:** SEC EDGAR Form 4 is the primary evidence, including the actual accepted/access time and original filing accessions. SEC publishes quarterly normalized Form 3/4/5 extracts, which are convenient for historical research but **not** a live disclosure feed. EDGAR submissions JSON provides filing index metadata, not a fully parsed transaction feed. To use live Form 4 facts, retrieve and parse their structured originals with verified acceptance timestamps. For research, distinguish open-market code P/acquired A/non-derivative/direct ownership from sales, option exercise, gifts, transfers, 10b5-1 preplans, amended/duplicate filings, incorrect issuer attribution, missing data and trades only publicly disclosed later. An insider's purchase does not establish positive expected returns.
4. **OpenInsider or comparable tools:** optional *candidate-lead sources*, not trading authority or the canonical timestamp. Never circumvent access controls, ignore site usage terms, rely on scraping without permitted reuse or accept the vendor date as the first tradable knowledge time. Independently resolve every promising lead to the original regulatory filing; store URL, accession, source ID, version and observed timestamp. A vendor/insider feed may be down or delayed; it must not be silently replaced with synthetic evidence.

SEC source references:
- [Insider Transactions Data Sets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets)
- [EDGAR Application Programming Interfaces](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [SEC fair-access developer resources](https://www.sec.gov/developer)

## Selection discipline and institutional research gates

**Stage A — universe eligibility:** jurisdiction, venue, equity share type, active listing, instrument/borrow permission, historical coverage, price integrity, market cap, dollar liquidity, spread, exchange/calendar rules and account permissions. Do not rank securities on evidence received after the decision timestamp.

**Stage B — deal discovery:** rank review leads based on independently sourced value, quality, momentum, institutional flow and insider accumulation evidence. The current narrow `scout_equities` routine only ranks *research workload* by a transparent combination of liquidity, spread and eligible Form 4 lead count. **Its ranking is not alpha and is not a backtested strategy.** No return forecast or apparent bargain claim is made. Future financial metrics must be adjusted for reporting latency, sector comparability, price revisions and corporate actions.

**Stage C — defend the hypothesis:** transaction costs, spread/impact/commissions, borrow and availability for shorts, out-of-sample walk-forward, multiple-testing accounting, DSR/PSR and null-family calibration, PBO, forward/fundamental as-of data, universe/survivorship, regime transitions and independently reviewed replicates. Every attempted candidate, score, threshold and strategy combination belongs in the trial ledger; do not optimize on the final holdout. New discovery paths count as an additional selection layer and increase multiple-comparison burden.

**Stage D — execution gate:** the research shortlist is never an order. Only after risk-approved target weights, market quote freshness, broker-qualified balances, pending-order reservations and operator authority may a bounded order be created, journaled, routed and reconciled. Any missing source/account/risk proof freezes new exposure. A high-aggression profile may widen risk *within fixed hard caps* only. Real-money activation is a separately reviewed product.

## Current code and known limits

- `src/lab/discovery.py`: typed as-known market snapshots, typed primary-source Form 4 records, explicit vs automatic modes, deterministic bounded screening and triage scoring, fail-closed no-data results; always `research_only=true`, `orders_authorized=false`.
- `tests/test_discovery.py`: empty watchlist/no data behavior, exact explicit universe, timestamp lookahead guards, blocked insider transaction types/plans, deduplication, stale/liquidity/market cap/risk boundaries, deterministic ranking and zero-order permissions.
- **Not delivered:** automatic provider discovery of all US listings; SEC Form 4 parser/ETL, legal source availability, market fundamental scoring, calibrated profitable strategy, authenticated feed/API, sector-neutral constraints, dashboard implementation, integrated portfolio builder, startup handoff or broker execution. This is deliberately not called plug-and-play market scouting yet.

## Release criterion (new)

An empty stock selection must resolve to a frozen, timestamped, provenance-linked research universe built from a real qualified provider. Results and rejection reasons are visible and reproducible; the system can legitimately select zero stocks. Insider-derived leads must resolve to official filings with independently checked timestamps. The end-to-end discovery→research approval→portfolio/risk→simulated/paper execution workflow must be field-tested, including missing-source and no-candidate cases. The implementation **must not bypass existing stock-selection, authorization, statistical or safety gates**.
