# BTCUSDT frozen failed-retest short — first historical result (2026-11-05)

**Verdict: FAILED. Retrospective research only; NOT_QUALIFIED.** No protected holdout was read and no trading deployment is authorized.

## Source and provenance

This note transcribes the operator's Termux output from the first **completed** v2 research replay. It is a descriptive summary, **not** an independently reproduced result or the authoritative machine-readable ledger. The authoritative evidence is the operator's durable `lab.trials` record and pinned funding and candle/OI input fingerprints; read those before asserting exact data/code identity.

- Repository branch: `chatgpt/codex-maintenance`; frozen candidate `btc-failed-support-retest-short-frozen-v1`.
- Registered v2 generation: family ID `5d0b3d91326a489491ea8b0e93d897dc`, immutable plan SHA-256 `443c08ad82562e2981e9c933b1a4952779a3f878d3b9aa97b4adddc456b1dc27`.
- Fixed BTC holdout start: **2026-05-08T00:00:00Z**; `holdout_used=false`.
- Historical observed study span: **2024-01-01 through 2026-05-07**, 858 UTC days.
- Exact funding-event SHA-256: `dc2d1b10da620664285fd0b25a45a1522214bf8af52d257ab36e2bc348eb2a8c`.
- Before registration, **10 older research attempts** were preserved. The v1 replay was recorded as a failed 11th attempt after rejecting an otherwise permissible pre-holdout daily archive boundary-stamped candle. The v2 replay is the next completed attempt (12th if no concurrent attempts occurred); verify this count in the local ledger. The unchanged strategy hypothesis was not retuned.

## Actual v2 results

| Measure | Value |
|---|---:|
| Completed trades | **15** |
| Active trade days | 15 |
| Gross research days | 858 |
| Win rate | **20.0% (3/15)** |
| Mean net trade | **-47.079369 bps** |
| Median net trade | **-71.452660 bps** |
| Modeled research portfolio annualized return | **-3.540572%** |
| Modeled research portfolio Sharpe | **-1.273469** |
| Modeled research portfolio maximum drawdown | **8.544154%** |
| Daily Newey–West t | **-1.734562** |
| Unweighted sum of net return across trades | **-706.19054 bps** |

**Caution:** The sum of individual trade returns is not the compounded or notional-weighted portfolio return. The negative Sharpe, annualized return and maximum drawdown are from the implementation's modeled daily equity series over the full 858-day span, including days without trades. They are **not** a calibrated estimate of a deployable trading strategy.

### Frozen trigger accounting

| Count | Events |
|---|---:|
| Breakout candidates | 127 |
| Retest events | 60 |
| Confirmations | 15 |
| Completed trades | 15 |
| No retest | 35 |
| No confirmation | 18 |
| Support reclaimed | 59 |

These are **state-machine event counters**, not independent statistical observations or a single set of disjoint reasons. Only **15 trades across 858 days** were generated (approximately 6.4/year). The inference/calibration hurdle would remain open even with positive returns.

### Execution cost stress

| Fixed taker fees per side | Slippage per side | Round-trip nominal friction | Trades | Mean net return | Annualized model return | Model Sharpe |
|---:|---:|---:|---:|---:|---:|---:|
| 5 bps | 5 bps | 20 bps | 15 | **-47.079369 bps** | -3.540572% | -1.273469 |
| 5 bps | 10 bps | 30 bps | 15 | **-57.092924 bps** | -4.187971% | -1.419675 |
| 5 bps | 20 bps | 50 bps | 15 | **-77.120034 bps** | -5.482771% | -1.647859 |

Funding covered **one unambiguous settlement**, received in one trade, with **+0.657594 bps summed across all 15 trades**. The reported funding interval ambiguity and uncertainty width were zero. Funding is negligible against the observed loss and does not rescue the candidate.

### Descriptive comparison with original crash-breakdown short

The original immediate-breakdown strategy lost **-106.3847 bps per trade across 32 trades**. The new failed-retest short lost **-47.0794 bps per trade across 15 trades**, a numerical difference of about **+59.3 bps per trade**. **Do not interpret that difference as a validated advantage.** The strategy rules differ, trades and exposure schedules differ, time in market differs, and the failed-retest hypothesis was designed after observing the original negative results. No matched tradable counterfactual or independently qualified test has established superiority.

The 2:1 stop-distance reward-to-risk design would require a theoretical **33.3%** win rate at zero costs for symmetric realized payoffs. The realized 20% win rate is below that rough benchmark, but real outcomes depend on time exits, gap/stop handling, fees, funding and unequal realized gains/losses. The direct observed **negative mean net** is the decisive descriptive stopping condition.

## Decision and next engineering priorities

1. **Retire this frozen candidate from the profitability/qualification track.** Preserve all registered generations, failed replay and completed v2 evaluation. Do not tune thresholds, retest windows, stops, reward ratios or data filtering against the same retrospective history.
2. **Keep the protected 2026-05-08+ BTC holdout closed.** An underperforming strategy does not justify sampling the holdout, and this historical period has already been inspected in the project.
3. **Do not declare the initial-crash strategy superior or inferior using these two unconditional averages.** Record them as separate, non-comparable descriptive screens.
4. **Focus the next research work on the general research system**, especially stronger source/coverage diagnostics, economically realistic fill/risk model qualification, and a fresh preregistered dependence-aware inference method that passes independent calibration. The earlier method failed its required error-control tests.
5. Any genuinely new bearish hypothesis must have **a separate frozen plan, a newly appended registration, and a truly independent evaluation protocol** before statistical claims. No paper/live-trading promotion is justified by this run.

This note is intentionally a **failure report**. It does not replace the local evidence ledger, alter the frozen strategy or change the v2 source identity.
