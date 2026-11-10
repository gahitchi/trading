# BTCUSDT bearish ensemble v1 — first registered historical result (2026-11-10)

**Decision: FAILED modeled net-profitability screen. RETROSPECTIVE_UNQUALIFIED.**

This note transcribes the user's successful Termux evaluation output. It is a
research handoff, NOT an independent reproduction and NOT the authoritative
trial ledger. The complete machine-readable result, input hashes, code identity,
and attempt IDs remain in the operator's durable
`/root/trading-termux-tests/results` directory.

## Frozen provenance

- Model: `btc-bearish-ensemble-ohlc-v1`, short-only.
- Source branch at evaluation: `chatgpt/codex-maintenance` (code and 41 synthetic tests previously validated; no market profitability was validated by those tests).
- Registered family ID: `74c63bc384ad41fe8e8d01a0adbf138d`.
- Registered plan SHA-256: `42fd73ffc8d458ab812f59b1b6ce73743059adc1afd81678cc17b6ad0a08da7f`.
- Prior research history retained at new registration: 12 entries.
- BTC protected cutoff: `2026-05-08T00:00:00+00:00`; reported `holdout_used=false`.
- Recovered original local caches: 44 BTC kline parquet periods and 1,038 OI parquet periods (including later protected periods, which the guarded loader must never open during this study).
- The evaluation command appears **twice** in the operator's transcript. The number, identity, inputs and outcomes of the newly persisted attempts must be confirmed from `lab.trials.history` before any statement about independent repetitions.

## Observed descriptive report

| Statistic | Observed |
|---|---:|
| Status | `RETROSPECTIVE_UNQUALIFIED` |
| Signals | 857 |
| Completed modeled shorts | 857 |
| Valid completed one-minute candles | 1,233,239 |
| Quarantined candles | 2,280 |
| Halted / unresolved after entry | false / 0 |
| Stop exits | 442 |
| Target exits | 215 |
| Time exits | 200 |
| Mean net modeled return per trade | **-20.516226618265684 bps** |
| Arithmetic sum of per-trade net return fractions | **-1.758240621185369** |
| Qualified | false |
| Holdout used | false |

Stop/target/time shares are approximately 51.6% / 25.1% / 23.3%.
The negative sum of returns is **not** a portfolio return, loss percentage
or compound equity curve. Each trade is denominated separately and the
replay does not maintain funded marked-to-market capital, actual funding
settlements, order-book fills, partial execution, financing or live latency.

## Interpretation

The strategy is **negative on its historical modeled economics** at the
frozen assumptions (5 bps taker fee and 5 bps slippage per side, 2 bps
round-trip spread, plus 1 bps adverse funding reserve). These are estimated
cost inputs, **not authenticated venue fee or fill evidence**. Its hypothetical
2:1 target is not a guaranteed realized payoff; frequent stops, time exits,
gaps, fees and slippage matter. The BTC history has been studied repeatedly,
so even a favorable retrospective result would not establish a prospective
edge or justify opening the protected holdout.

**Action: retire v1 from the profitability qualification lane.** Preserve the
original failed experiments, current family registration, any duplicate
evaluation attempts, all source-fingerprints and the protected partition.
Do not mutate the already registered model thresholds, repeat sweeps on the
same data, promote signals into a paper/live executor, or reinterpret raw
trade-return sums as daily P&L.

## Next engineering work (without re-running this candidate)

1. Audit the **two possibly duplicated evaluations** using the authoritative
   `history()`: statuses, exact family links, fingerprints, input digests,
   result equality, and the current count of attempts.
2. Verify that the full `research_family` history and prior negative BTC
   attempts remain intact.
3. Build a separate marked-equity / margin / actual-funding accounting
   model for prospective, newly frozen research. The present OHLC proxy does
   not supply daily equity returns or calibrated intrabar execution.
4. For genuinely new short candidates, identify independent pre-registered
   data and forward-evaluation protocols, including different instruments
   with their own holdout source coverage, cost stress and risk bounds.

No new code behavior or frozen strategy parameters are changed by this note.
