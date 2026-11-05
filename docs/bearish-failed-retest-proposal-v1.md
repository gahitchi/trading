# Research proposal — failed-retest bearish structure break (v1)

**Status: frozen candidate logic accepted in 17/17 focused Termux tests on commit `7c14cb0`. Further causality fixes, guarded plan/register/evaluate CLI and integration tests are committed but awaiting Termux validation. NOT run on historical BTC, NOT registered in the user's research family, and NOT statistically qualified.**
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

The engine has **no live-market ingestion, no optimization interface,
and no CLI command capable of reading historical research data**.
Only injected bars whose last timestamp is strictly before an
explicit UTC holdout boundary are accepted. The 2026-05-08 protected
holdout is not touched by unit tests. Source pinning, safe registration,
and a durable dedicated evaluation CLI must be completed and
validated separately before any historical research run.

## Durable guarded research workflow (not yet user-accepted)

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

**Do not register a plan automatically:** `lab research register`
is a one-time immutable registration for the whole results directory,
not a strategy-only setting. It retains earlier completed and failed
evidence as retrospective, which blocks clean prospectivity. If the
directory already contains another research family, do not replace
or bypass it. A registered new candidate is still being diagnosed
retrospectively on the viewed BTC research period; passing it cannot
qualify protected holdout access or declare a profitable edge.

After the new Termux tests pass, generate and inspect a plan first:

    PYTHONPATH=src:. python -m lab.bearish_retest_research \
      --config ~/trading-termux-tests/lab.toml \
      --funding-plan ~/trading-termux-tests/btc-funding-preholdout-plan.json \
      --plan-out ~/trading-termux-tests/btc-failed-retest-plan-v1.json

Registration and `--evaluate` should be separate deliberate operator
actions only after verifying the plan, the existing family status and
the complete 175-test regression run. This new pathway has not
been externally tested or used against historical BTCUSDT yet.

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

If the fixed initial candidate has negative net research returns at
the base 20-bp nominal round-trip cost, or has inadequate independent
sample size/coverage, record it as a failed candidate. Do not
repeatedly modify parameters inside the same frozen hypothesis.
Any successor is a *new* specifically registered search member.
A favorable result is likewise insufficient without source
provenance, comparison fairness, validated inference, realistic
fills and an untouched independent validation period.
