# Short-horizon trading objective and daily evaluation policy

**Decision: 2026-11-05.** This supersedes the *business objective*
of optimizing annualized research returns for the main fast trading
track. It does not retroactively edit frozen experiments, previously
registered plans, legacy data partitions, statistical calibration,
or the 2026-05-08 UTC protected BTC holdout.

## Actual goal

Build a short-horizon, institutionally disciplined, **paper-first**
crypto perpetual trading/research system that seeks **positive
compounded net equity growth across profitable, losing and flat
UTC days**. The user is comfortable with a highly variable daily
return distribution: one day might earn €0.20, a later day $120,
and another day lose $80. These are **illustrative net outcomes,
not a promised frequency, minimum daily profit or risk budget**.
Reconcile currency differences at documented, timestamped FX rates
and disclose conversion friction before aggregating returns.

**Performance priority:** first ensure that the aggregate of net
profits is greater than the aggregate of net losses and costs
over independently evaluated periods, while losses and drawdowns
remain within a precommitted risk budget. Second, evaluate how
often opportunities arise and how consistently cumulative equity
recovers from setbacks. Winning-day fraction, €0.20/€0.50
milestones, daily mean and median returns are useful secondary
distributions, **not hard success quotas**. An isolated large
winning day must not conceal unacceptable downside or sparse
market participation.

**Partial reinvestment:** a user-configurable fraction of
**audited, realized net profits** is allocated to future
deployable trading capital, with the remainder assigned to a
separate retained-profit reserve. Reinvestment changes future
position-sizing capacity, **not the historical P&L, total equity,
or quality of the trading edge**. The percentage is unspecified
until the operator sets it; do not silently default to 50%, 100%,
or any other live policy. See
[profit allocation and compounding contract](profit-reinvestment-accounting.md).

Positive long-run net expectancy and controlled capital growth
are research objectives, **not** contractual guarantees of
profit or recovery after a losing day. A profitable day is
counted only after all actual modeled trading frictions, marked
inventory, margin/funding effects and any required currency
conversion. A zero-trade day has zero net trading P&L (unless
open inventory, carrying cost or fees remain), never a fictional
daily gain. Cash loss limits may stop a session, and the correct
decision may be to make no trade rather than force low-edge fills.
The system must not use martingale sizing, loss-chasing, increasing
leverage or artificial daily trade quotas to try to guarantee
a positive calendar-day close.

Annual percentage return, funding carry and multi-day holding
performance are **secondary diagnostics** for the main fast
track. Daily equity curves, cumulative net P&L, loss
distributions, drawdown and recovery matter most. A positive
headline return can conceal prolonged inactive periods,
unacceptable interim losses or a strategy whose rare winners
cannot be reproduced.

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
2. **Mean, median and distribution of net daily returns** across
   *all eligible days*, including positive/negative/flat days and
   zero-trade days. Optionally report days with **net profit
   >= €0.20** and **>= €0.50** as illustrative milestones
   after a traceable FX conversion; they are not acceptance
   quotas. Track both small and large positive and negative days.
3. **Active-day incidence**, trades/day (all days and active days),
   number of independent active dates, and max consecutive
   zero-trade days. An isolated winner cannot make a strategy
   look like a daily-income system.
4. **Risk and recovery:** total cumulative net P&L, compounded
   marked equity, gross winning-day gains, absolute losing-day
   losses, profit factor (when defined), maximum drawdown,
   largest loss day, negative-tail quantiles, duration to
   recover prior equity highs, and leverage/margin exposure.
   Include costs, unrealized P&L, positions carried across
   midnight and the gap between actual equity and available
   risk/deployable capital.
5. **Execution:** round-trip net bps after fees/spread/slippage,
   maker vs taker fill rate, adverse selection, rejected/partial
   fills, live-feed age, order acknowledgement/cancel latencies,
   and 50/150/300/1000ms replay sensitivity.
6. **Operational viability:** coverage hours/days, downtime,
   stale-book gaps, missed/duplicate source events,
   unreconciled positions, order/state restart tests and
   whether the strategy is adequately active when the user's
   computer is actually online.
7. **Capital allocation and evidence:** realized net profit
   available for reinvestment, configured reinvestment fraction,
   amount reallocated to deployable capital, amount reserved,
   daily transfers/withdrawals, mark-to-market total equity,
   and precommitted risk/exposure limits. Retain source, code,
   runtime and allocation-policy hashes; all attempted
   settings, study windows, boundaries and qualification state.

Display **daily net P&L and the cumulative marked-equity curve**
together, followed by the winning/losing-day distributions,
drawdown, recovery and capital allocation breakdown. Annualized
figures, conventional Sharpe and funding yield may appear as
secondary diagnostics only, clearly labeled as statistical
projections from the full daily sample.

## Acceptance gates (not yet attained)

These are **decision criteria**, not claims that a daily edge has
been discovered. Before admitting a proposed strategy into a
protected holdout or a supervised paper campaign:

- **Daily-relevance gate:** opportunity and executed round-trip
  counts must characterize normal days rather than only rare
  crashes. Report active-day frequency, positive/negative/flat
  day rates, all-day daily net expectancy, variable profit/loss
  magnitudes and coverage-adjusted inactivity. Small winning
  days count, but are not worth a forced trade; no artificial
  frequency quota can manufacture opportunities.
- **Net profitability gate:** positive **cumulative net
  marked-equity growth and positive mean net UTC-day P&L**
  across *all eligible days* after fees, funding, adverse fills
  and partial-fill accounting. Record whether **aggregate
  winning-day gains exceed aggregate losing-day losses**,
  and whether the edge survives feasible latency and cost
  stresses. The magnitude and tail of the losing days, plus
  recovery duration, matter more than attaining a fixed
  winning-day percentage. No 100%-winning-day requirement,
  forced trade, loss-chasing or automatic risk increase is
  allowed. Retain flat and failed days in the denominator;
  a positive point estimate alone is insufficient.
- **Risk and reinvestment gate:** measured drawdown, worst-day
  loss, leverage, open exposure, kill switch and liquidation
  stress stay inside separately precommitted capital limits.
  Profit allocation is applied **only to realized net profits**;
  portfolio losses decrease actual equity, and reinvestment
  cannot bypass order, exposure, leverage, daily-loss or
  maximum-deployable-capital limits. Test both fixed-capital
  and fractional-reinvestment baselines against identical
  signals and fills to isolate sizing effects. No reinvestment
  fraction or risk budget is implied by this document.
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

1. **Coverage, daily equity and reinvestment accounting:**
   establish one UTC-day realized/unrealized net-P&L schema
   shared by replay and paper, reconcile funding/fee/FX flows,
   explicit zero-trade days, complete equity snapshots,
   deployable-versus-reserved cash, configurable partial
   realized-profit allocation, loss debit, risk caps and
   auditable transfer events. Add ledger invariants and
   fixed-capital versus compounding tests before running
   a new candidate.
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
