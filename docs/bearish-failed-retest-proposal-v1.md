# Research proposal — failed-retest bearish structure break (v1)

**Status: FAILED.** The operator accepted 190/190 portable regression tests, registered the v2 corrected replay (family `5d0b3d91326a489491ea8b0e93d897dc`), and completed the first pre-holdout historical evaluation: **15 trades, -47.0794 net bps/trade, -3.5406% annualized model return, 20% win rate, Sharpe -1.2735, 8.5442% max drawdown**. No holdout use. **NOT_QUALIFIED**; the documented negative-return stop criterion applies. Exact source, sensitivity and interpretation: [v1 completed failure report](bearish-failed-retest-result-v1.md).
2026-11-05. This idea is documented after inspecting the losing BTCUSDT
`crash breakdown` result. **It is not a prospectively independent
discovery relative to that inspected dataset.** Neither the former
2024-01-01..2026-05-07 research series nor the locked 2026-05-08+
holdout can be claimed fresh confirmation. Any eventual independent
qualification must use a separately precommitted validation protocol
and genuinely unseen data; do not unlock the crash holdout to test it.

## Hypothesis and difference from the failed preset

The old rule shorts the immediate 3%/60-minute sell-off if volume spikes
and contract OI increases. Its short returns were negative across
all studied forward horizons, so retuning its threshold or stop against
those outcomes would risk overfitting.

A separate hypothesis is that *failure to reclaim an already broken
24-hour support level* after a retest identifies bearish continuation
more effectively than selling the initial impulse.

This is a new price-structure event with a distinct state machine and
confirmation sequence; there is no inference from the old results that
it will work. Its parameters below are **design assumptions** and
must not be modified to improve retrospective performance without
recording a new candidate and expanding the search family.

## Frozen first candidate rules, all in UTC minute-end time

1. **Breakout event:** at completed candle `T0`, compute
   `L = min(low[T0-1440min .. T0-1min])` on a complete 1-minute
   grid. Require `close[T0] <= 0.997 * L` (0.30% breach) and
   last-60-minute quote volume >= 2 times the median of the
   previously completed hourly quote-volume buckets of the preceding
   seven UTC days (exclude the current 60-minute bucket and exclude
   any partial/invalid bucket).
2. **Retest window:** watch only the next 60 completed one-minute bars.
   The first completed bar `Tr` whose high reaches at least
   `0.999 * L` (within 0.1% below broken support) is a retest.
   Reject the setup if any completed candle closes above `L`
   before the bearish confirmation.
3. **Failed retest confirmation:** within the five bars following
   `Tr`, a close below `0.999 * low[Tr]` confirms rejection.
   Cancel the event if the window expires, or if support has already
   been reclaimed (`close > L`).
4. **Short entry:** submit no order until the confirmation minute has
   completed. Model the opening print of the **second** subsequent
   one-minute bar (one full minute of artificial delay, consistent
   with the old crash-study delay convention). No fills before the
   signal, no backdating, no partial-minute price access.
5. **Exit and risk:** for a short entered at `P`, stop reference
   `S = 1.001 * max(L, high[Tr])`. If `S <= P`, the setup is
   invalid and produces a *counted canceled entry*, not a new
   selectable strategy. If valid, target `P - 2*(S-P)`
   (2:1 nominal reward-to-stop-distance). A stop and target touched
   in the same bar is a stop (conservative). Time exit at most 240
   minutes after entry. The existing price-gap, fee/slippage and
   exchange-funding policies apply and must be tested.
6. **Portfolio:** at most one BTCUSDT candidate/trade at a time.
   Keep the existing 50%-annualized-volatility targeting and 2x
   notional cap for exploratory comparability. Block signals when
   candle gaps, invalid price fields or OI-source quality exclusions
   prevent causal evaluation; OI direction itself is **not** a
   strategy trigger.
7. **Data:** Binance USD-M BTCUSDT minute klines and exact
   settlement-mark funding, with the frozen holdout never accessed
   during implementation or exploratory selection. All inputs
   and software versions require durable fingerprints.

## Execution timestamp and source-grid convention

The archived `lab.crash_data` Binance klines use `ts = open_time + 60 s`,
i.e. their index is the **END of each candle**, not its open.
The frozen engine enforces a complete UTC one-minute end-stamped
grid, rejects nonfinite/inconsistent OHLC and unsourced
`oi_invalid` quality flags, uses exactly the previous completed
24 hours for support, and delays entry until the opening of
the second candle after the completed confirmation bar.

The historical volatility reference must be positive, finite and
entirely lagged before the breakout. An unavailable or zero-volatility
reference cancels the candidate rather than silently allocating its
maximum position weight. Missing candles cause a fail-closed result.
The seven-day reference quote-volume hours are grouped by candle
*start* time and end strictly before the first minute in the
latest 60-minute breakout window, to avoid lookahead or overlap.

The **pure strategy engine** has no live-market ingestion or
optimization interface. Its separate guarded research CLI can open
only source archives ending no later than the accepted research
boundary, reserves attempts before input access, and rejects any
injected strategy bar at or beyond the holdout boundary.
The 2026-05-08 protected holdout is not used by unit tests.

## Durable guarded research workflow (v2 executed; no retuning)

`python -m lab.bearish_retest_research` has two deliberately
distinct operations:

- `--plan-out /new/path.json` validates the existing UTC holdout
  and the v2 audited funding source **metadata** and writes exactly
  one frozen prospective family member in a non-overwriting file.
  It does not open minute candles or OI Parquet archives, simulate
  signals, create a new partition or log a research attempt.
- `--evaluate` requires the precise frozen member to already be
  registered through `python -m lab.cli --config ... research
  register --plan /new/path.json`. It checks the entire registered
  source/runtime identity; if the member is missing or the code
  changed, it fails before opening historical source archives.
  Upon authorization it durably reserves a new `crash-study`
  attempt, then audits pinned Binance funding, loads only
  pre-boundary candles/OI, checks the OI quantity/quality schema,
  fingerprints all three input streams, runs the frozen candidate,
  applies the pessimistic funding model, and reports trade counts
  and fixed-cost stresses. No optimization/parameter controls exist.

**Do not register a plan automatically.** `lab research register`
now freezes an append-only registration generation after external
validation of the registry upgrade; it does not replace or amend a
prior registration. Any later generation is permitted only after all
members of the previous generation have been attempted and no
research attempt remains reserved. The old plan and complete trial
history remain retained, linked, and blocking as appropriate.
The first frozen family was registered with **10 historical attempts**
on 2026-11-05. Its v1 evaluation was durably recorded as a
**failed 11th attempt** because the loader returned a candle
labelled exactly `2026-05-08T00:00Z`, which represented the
final 23:59–00:00 minute of May 7, still stored in the permitted
May 7 daily archive. Unlike the older crash study, the v1
research CLI rejected the whole archive instead of dropping
that single boundary-stamped candle. No failed-retest strategy
P&L was produced or selected.

The corrected **v2 research replay** retains all strategy thresholds,
the same data-partition policy and full trial history. It allows
source archives already approved by the filename-level UTC cutoff
and drops just the row at exactly the boundary before fingerprinting
or strategy simulation. A row strictly **after** the boundary
still aborts. This fix changes the software identity and research
policy, so the original registered v1 plan must not be rewritten
or retroactively re-evaluated with modified source. Instead,
register a successor generation **after** the expanded tests
pass, preserving the failed v1 attempt as evidence. These
retrospective attempts cannot statistically qualify the candidate
or authorize live/paper trading.

After the new Termux tests pass, generate and inspect the
**separate v2 replay plan**, never overwriting the original v1 plan:

    PYTHONPATH=src:. python -m lab.bearish_retest_research \
      --config ~/trading-termux-tests/lab.toml \
      --funding-plan ~/trading-termux-tests/btc-funding-preholdout-plan.json \
      --plan-out ~/trading-termux-tests/btc-failed-retest-archive-fix-v2.json

The operator completed these steps: v2 plan comparison against v1
confirmed that only `study_policy` and `data_policy` changed;
registration succeeded with previous trials retained; the first
v2 historical run finished with 15 trades and negative net
returns. The exact outcome and stop decision are archived in
`docs/bearish-failed-retest-result-v1.md`. The original v1
registration and failed attempt remain evidence. **Do not
generate a tuned v3 candidate from this same inspected result.**

## Qualification rules to freeze before the first test

- Implement this state machine independently; do not retrofit the
  old `crash_study.signals` with optimized numeric thresholds.
- Unit-test every causal transition, gaps, repeated retests,
  cooldown, stop/target priority, late entries, exchange funding,
  research partition and rejected entry reason.
- The first study is counted as **one new research member**; prior
  failed crash studies, incomplete attempts and any variants count
  as existing evidence and cannot be hidden.
- Register the full planned family with `lab research register`
  **before any execution** when the candidate implementation,
  precise params and evaluation window are frozen. The software now
  permits `crash-study` as a family member kind. A live registration
  is not performed by placing this file in Git.
- A retrospective result on inspected BTCUSDT history is only a
  *development diagnostic*; it is not out-of-sample evidence.
- An independent dependence-aware inference procedure must first
  pass its preregistered calibration suite. The current v1 daily
  joint-inference procedure failed independent calibration and is
  not an admissible statistical gate.
- No holdout access, paper-trading promotion, leverage increase,
  live orders or profit claim is allowed merely because a
  descriptive historical backtest is positive.

## Stop criteria

**Triggered on 2026-11-05:** the first fully evaluated candidate
had mean net return **-47.0794 bps per trade** across 15 trades.
The strategy should not advance toward paper trading or
statistical qualification. See the completed failure report.

If the fixed initial candidate has negative net research returns at
the base 20-bp nominal round-trip cost, or has inadequate independent
sample size/coverage, record it as a failed candidate. Do not
repeatedly modify parameters inside the same frozen hypothesis.
Any successor is a *new* specifically registered search member.
A favorable result is likewise insufficient without source
provenance, comparison fairness, validated inference, realistic
fills and an untouched independent validation period.
