# Short-horizon trading objective and daily evaluation policy

**Decision: 2026-11-05.** This supersedes the *business objective*
of optimizing annualized research returns for the main fast trading
track. It does not retroactively edit frozen experiments, previously
registered plans, legacy data partitions, statistical calibration,
or the 2026-05-08 UTC protected BTC holdout.

## Actual goal

Build a short-horizon, institutionally disciplined, **paper-first**
crypto perpetual trading/research system that can discover opportunities
on most active days, with an ultimate objective of **positive net
daily P&L over a sufficiently large, independently validated sample**.
Daily profit is a target for *expected performance*, **not** a
contractual guarantee of gains every day. Cash loss limits may stop a
session, and the correct decision may be to make no trade.

Annual percentage return, funding carry and multi-day holding
performance are **secondary diagnostics**, not the optimization target
for the main fast track. One positive annualized return can conceal
prolonged periods without trades, excessive drawdown, or poor
performance on ordinary trading days.

## Trading speed and position-holding horizon

- **Initial research operating point:** market observations may be
  continuous from WebSocket trades + L2 books, while the slower
  signal decision clock is **one minute**, with candidate holding
  periods of roughly **1 to 15 minutes**. More granular decisions
  may be researched only after timestamp quality, fill simulator
  and actual execution latency are qualified.
- **Parallel exploratory horizons:** **30–60 seconds**, **1–5 minutes**,
  and **5–15 minutes**; each is a distinct, counted candidate cohort
  to register *before* looking at comparative outcomes. These are
  hypotheses, not an invitation to choose the best after inspecting
  the same history. If recorded book/trade data are insufficient for
  subminute order fills, label the experiment unavailable rather
  than substituting minute OHLC or pretending accurate fills.
- **Strategy direction:** initially prioritize bearish market
  continuation, breakdowns and other genuinely shortable
  opportunities. The direction alone is not an edge; a wrong short
  can lose quickly, and gaps/liquidations still matter.
- **Venue and order constraints:** crypto USD-M perpetuals, realistic
  maker/taker fees, spread, market impact, quote-age constraints,
  funding where applicable, partial fills, adverse selection,
  rejected orders and maximum inventory exposure. Do not assume
  free/instant executions or infinite capacity.
- **Latency:** optimize end-to-end measurements at home-network
  conditions, and stress at **50 ms, 150 ms, 300 ms and 1,000 ms**.
  If profitability only exists at unattainable latencies, reject
  that strategy. Do not compete in submillisecond arbitrage/HFT
  against colocated institutional systems.
- **Engineering:** retain the independent design; prioritize the
  already-running Rust hot path for market ingestion/order state,
  with Python permitted for offline analysis and reproducible
  experiments. Benchmark the current implementation *before*
  claiming Rust is faster in deployment. Orders remain paper/demo
  until every qualification and risk gate has passed.

## Primary daily scorecard (UTC calendar days)

Every evaluation must report a complete **UTC daily equity and P&L
series**, including **zero-trade days**. Reconcile marked equity
(realized + unrealized, not just closed-trade outcomes) to the order,
fee, funding, financing, margin and funding-settlement ledger.

Daily scorecard fields:

1. **Net P&L per UTC day** in account currency and percent of
   start-of-day allocated capital, with an unambiguous choice of
   open/close valuation marks and daily capital denominator.
2. **Mean and median net daily return** across *all eligible days*,
   plus fraction of **positive/negative/flat days**, including days
   that produced zero trades.
3. **Active-day incidence**, trades/day (all days and active days),
   number of independent active dates, and max consecutive
   zero-trade days. An isolated winner cannot make a strategy
   look like a daily-income system.
4. **Risk:** largest loss day, daily and cumulative maximum drawdown,
   tail losses, stops, leverage/margin use, adverse price excursions
   and exposures carried across midnight.
5. **Execution:** round-trip net bps after fees/spread/slippage,
   maker vs taker fill rate, adverse selection, rejected/partial
   fills, live-feed age, order acknowledgement/cancel latencies,
   and 50/150/300/1000ms replay sensitivity.
6. **Operational viability:** coverage hours/days, downtime,
   stale-book gaps, missed/duplicate source events,
   unreconciled positions, order/state restart tests and
   whether the strategy is adequately active when the user's
   computer is actually online.
7. **Evidence:** source, code and runtime SHA provenance, all
   attempted parameters/configurations, exact trial/horizon
   family registration, study window, UTC time boundaries,
   protected holdout state and inference qualification.

Display **daily** returns and loss distributions first. Annualized
figures, conventional Sharpe and funding yield may appear as
secondary diagnostics only, clearly labeled as statistical
projections from the full daily sample.

## Acceptance gates (not yet attained)

These are **decision criteria**, not claims that a daily edge has
been discovered. Before admitting a proposed strategy into a
protected holdout or a supervised paper campaign:

- **Daily-relevance gate:** opportunity and executed round-trip
  counts must be sufficient to characterize *ordinary days*,
  not simply a handful of rare crashes. Evaluate active-day and
  zero-trade-day distributions honestly; no artificial quota
  can force trades when signals are absent.
- **Net profitability gate:** positive **mean net UTC-day P&L**
  across *all eligible days*, after realistic funding, fees,
  adverse fills and partial-fill accounting; positive net return
  must survive plausible home-network latency and cost stresses.
  Retain losing days, nontrading days and all failed research
  configurations. A positive point estimate alone is insufficient.
- **Risk gate:** measured drawdown, worst-day loss, leverage, open
  position exposure, kill switch and collateral/liquidation
  scenarios meet a separately precommitted capital risk budget.
  No risk budget is implied by this document.
- **Statistical gate:** correct for multiple searched strategies,
  serially dependent daily outcomes and cross-symbol correlation;
  an *independently calibrated* inference method must pass the
  registered false-positive requirements, on genuinely unseen
  data. The existing daily v1 inference method failed independent
  validation and is **NOT_QUALIFIED**.
- **Execution gate:** backtest is supported by time-ordered L2/trade
  evidence and independently validated queue/fill/latency
  accounting, with simulator assumptions checked against
  subsequent supervised demo/paper fills. Reject edges smaller
  than realistic transaction friction.
- **No shortcut to live money:** daily-profit intent does not
  authorize live orders, increased leverage or early holdout use.
  The software remains paper/demo first and follows the project
  risk-governance roadmap.

## Evidence from retired BTC short candidates

- **Immediate crash breakdown:** 32 trades in the old BTC
  pre-holdout period, mean **-106.3847 net bps/trade**; funding
  and 16 matched descriptive placebos did not rescue the outcome.
- **Frozen failed support retest:** 15 trades in **858 UTC
  days** (about one trade every **57.2 days**), with **-47.0794
  net bps/trade**, a **20% win rate**, and negative realized
  research returns after cost and funding.
- These strategies are **too infrequent** to serve the daily
  objective, even apart from their negative economics.
  They should remain failed archived trials. Do not lower
  their filters, alter stop/target ratios, relax evidence
  requirements or retrospectively optimize them to generate
  more daily trades.
- Short-term microstructure taker screens have already shown
  predicted returns smaller than taker round-trip friction.
  Reusing those signals without an execution or market
  economics hypothesis is not a viable shortcut.
- Market making remains an available research track only where
  its adverse-selection-adjusted spread exceeds the real fill
  costs at achievable latency.

## Recommended development sequence

1. **Coverage and daily accounting:** establish one daily UTC
   realized/unrealized net P&L schema shared by replay and paper,
   a reconciled mark/funding/fee ledger, explicit zero-trade
   days, completeness/availability statuses and source hashes.
2. **Feasible market selection:** survey highly liquid shortable
   contracts and spread/fee structure; choose a small research
   universe based on ex ante liquidity and reliability, not
   backtest returns. Keep availability across Europe/US/Asia
   sessions measured.
3. **Short-horizon feed and execution audit:** capture reliable
   exchange L2/trades, reconstruct causal books, measure queue
   position and fill quality; run 50/150/300/1000ms stresses
   and verify realistic stop execution during jumps.
4. **Prespecify new research families:** independently freeze
   signal concept, all symbols, holding horizons, costs,
   historical development window and **daily scorecard** before
   new evaluations. Limit candidate multiplication; no
   winner-only reporting.
5. **Empirical development:** measure daily occurrence and
   net-of-cost profitability on research data. A fail remains
   a permanent trial; then seek a genuinely new independent
   validation period and a qualified inference method.
6. **Supervised paper then live only by separate approval:** audit
   day-to-day returns and outages first, and require risk
   qualification before any real-money access.

This defines a direction and evidence contract; it is **not** a
registered strategy, parameter search, changed holdout,
deployed execution module or a profitability claim.
