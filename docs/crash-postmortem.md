# BTC crash research postmortem — protocol v1

## Termux completed result — 2026-11-05

The operator ran the frozen pre-holdout BTCUSDT breakdown diagnostic,
producing `btc-breakdown-postmortem-v1.json`. This external acceptance
checkpoint reports:

- 32 BTCUSDT shorts; mean net **−106.3847 bps**; all 62
  strategy-held funding events were unambiguous in the prior run.
- Exit attribution: **17 stops** (mean **−319.8508 bps**),
  **1 target** (+780.1524 bps), **14 time exits**
  (mean **+89.5001 bps**). Mean duration **906.94 minutes**.
- Unweighted return components summed over the 32 trades:
  price **−2792.4061 bps**; fees **−321.3969 bps**;
  slippage **−321.3962 bps**; exact audited funding
  **+30.8894 bps**; net **−3404.3097 bps**.
  Price movement itself was unfavorable: −87.26 bps per
  trade before costs. All values are descriptive, not
  independent samples or qualified inference.
- Year breakdown (mean net): 2024, 15 trades,
  **−77.8585 bps**; 2025, 10 trades, **−204.5736 bps**;
  2026 research prefix, seven trades, **−27.2424 bps**.
- Every one of the 16 predetermined, same-month/hour,
  duration/weight-matched descriptive entry placebos had
  a greater mean return than the original strategy.
  The **original-minus-placebo** difference ranged
  from **−166.6561 to −32.7541 bps**, with median
  **−95.0158 bps**. The placebo holding durations
  were inherited from realized original exits, so they
  are not independent, tradable exit strategies and the
  differences are not p-values or confidence intervals.
- Durable attempt inventory: **10 total** (two completed,
  one failed, seven legacy); **nine distinct
  configurations/code versions**; seven records lack
  complete identified input/code provenance.
- Inference eligibility: **NOT_QUALIFIED**. The previously
  attempted independent joint-inference calibration failed.
  No holdout evaluation or strategy promotion is permitted.

**Interpretation:** adverse gross price moves and stop outcomes,
not funding or execution fees alone, explain the losing setup.
These data do **not** demonstrate an optimal exit revision.
The outcome supports retiring this particular initial-crash
entry rule from the qualified-candidate path. The
`docs/bearish-failed-retest-proposal-v1.md` concept
remains a *new, untested hypothesis*, not a tuned retest of
the unsuccessful preset.



Status: committed, **not yet externally tested on Termux**.

The predefined 2026-11-05 BTCUSDT breakdown experiment lost
−106.4 bps on average across 32 trades. Its 479-trade daily-entry
baseline is not risk/exposure matched. All old trial attempts remain
in the durable research ledger.

## Failure attribution

`lab.crash_postmortem` reconciles each trade's gross P&L, taker fees,
assumed slippage, audited funding and net P&L. It groups the loss
by exit reason and year, measures entry-to-exit duration/size,
and records 24-hour context available by the signal minute.
Full-bar high/low excursions include the entire exit minute; they
are not executable MFE/MAE or evidence of intrabar fill order.
Missing minute candles and inconsistent economics fail closed.

## Matched descriptive placebo

`lab.crash_placebo` freezes 16 independent seeds in source control.
For each original trade it samples a non-overlapping alternative
entry in the **same calendar month and UTC minute of day**, 3–14
days from the original. It preserves exactly the original trade
count, entry-notional weight and realized holding duration.
All 16 comparisons retain the same verified funding treatment and
taker-fill costs. Selection depends on candle availability, not
candidate price returns.

A placebo closes after the **original realized holding duration**
without recreating the stop/target logic. It is therefore a
**descriptive exposure comparison**, not a tradable matched strategy
or an inference-calibrated significance test. No best-seed selection
or parameter tuning is permitted.

`lab.crash_qualification` inventories all durable attempts,
including failed and legacy records. The prior independent
calibration of the v1 daily-inference procedure failed and blocks
statistical significance and protected holdout access.

## Termux regression tests

    cd ~/trading-journal-validation
    git fetch origin
    git switch --detach origin/chatgpt/codex-maintenance
    source ~/trading-venv/bin/activate

    PYTHONPATH=src:. python -m pytest \
      tests/test_crash_postmortem.py \
      tests/test_crash_postmortem_cli.py \
      tests/test_research_family.py \
      tests/test_crash_funding_ledger.py \
      tests/test_crash.py -v

Only after tests pass, execute the frozen diagnostic **once**:

    PYTHONPATH=src:. python -m lab.crash_postmortem_cli \
      --config ~/trading-termux-tests/lab.toml \
      --funding-plan ~/trading-termux-tests/btc-funding-preholdout-plan.json \
      --report-out ~/trading-termux-tests/btc-breakdown-postmortem-v1.json

The command reserves a research attempt **before** reading
market data; unsuccessful attempts remain counted. The report
file will not overwrite any existing report. Do not change its
fixed seed set to improve an outcome. If any placebo cannot
be matched safely, the attempt fails rather than relaxing its
research protocol.

## Next hypothesis

A distinct, **untested** bearish failed-retest idea is specified
in [bearish-failed-retest-proposal-v1.md](bearish-failed-retest-proposal-v1.md).
Its design followed inspection of losing BTC history. It cannot
qualify as prospective evidence on that previously viewed period.
It is not yet an executable strategy or registered in the user's
local prospective research-family ledger.

Protected BTC holdout: **2026-05-08 UTC onward, untouched**.
