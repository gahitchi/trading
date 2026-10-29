# Observed execution replay (roadmap A7a)

The `observed-flat-gap-v1` model uses NautilusTrader 2.0.0rc7, pinned in the dependency lock. The model name,
one-second book freshness threshold, submission latency and effective cancellation latency are included in
`fast-backtest` configuration fingerprints. This is execution-model validation on synthetic recordings;
it does not qualify a candidate, establish an edge, or complete A8b.

## Recording holes

Replay uses receive timestamps (`ts_init`). A silence greater than one second between complete books starts a
new observed segment. Every complete book must have both sides and an uncrossed spread; incomplete terminal
batches are rejected. Trades without a complete book at or before their receive time, no more than one second
old, are rejected. The threshold is a conservative project assumption, not proof of feed continuity.

The engine pauses at the preceding segment's last observed event, without running shutdown handlers or
advancing through the missing interval. Only then does the lab inspect the actual account and order state:

- Any open inventory makes execution and valuation across the gap unknown. The evaluation is unavailable.
- Any outstanding order also makes execution unknown, including submissions or cancellations in transit.
  A missing trade stream cannot establish whether a resting order filled or its cancellation arrived first.
- If both are absent, replay can resume only from a fresh book snapshot (`CLEAR` followed by a complete,
  usable book). The same account, trade history and risk manager continue; cash and prior losses are retained.

There is no anticipation of a future outage, forced close before it, free cancellation, inventory reset,
fill interpolation, or reset to starting capital. Rejected evaluations return no performance result. The CLI
logs these model rejections as unavailable attempts; holdout reservations remain consumed after rejection.
Recording every other kind of research failure, including process crashes, remains part of A8b.

This deliberately means a continuously quoting maker will often be unavailable on gappy recordings. Genuine
risk across an unobserved interval cannot be recovered from the available feed. Do not trim a window or drop
a session after seeing its outcome to remove inventory, losses or failed evaluations. A richer disconnect
model would need separately registered assumptions and sensitivity evidence before its results could qualify.

## Recording end

Each segment runs with the engine's streaming API, which pauses when its loaded data ends. The lab captures
results at the last observed event before disposing the engine. Replay shutdown does not submit orders.
Paper shutdown retains its existing cancellation and flattening behavior.

Trailing submissions and cancels remain outstanding if they have not arrived by the final observation. Open
positions remain open. The output and trial log retain terminal net quantity, outstanding order count and the
last complete book's valuation timestamp. They also separate:

- `cash_pnl`: account balance change, including realized P&L and paid commissions;
- `unrealized_pnl`: open positions marked with the shared risk manager's reference price;
- `pnl`: total marked net equity change, the sum of those two components.

Daily net equity includes the same terminal mark. Round trips and their descriptive t-statistic contain only
closed positions, so their sum need not equal total P&L. Marked equity is not a demonstrated liquidation price;
terminal inventory and outstanding orders require scrutiny before accepting A7/A9. A profitable subset of
closed trades cannot erase an unfinished losing position. Missing calendar-day returns still make the Sharpe
diagnostic unavailable under the existing daily-return policy.

## Latency and queue assumptions

`--latency-ms` is fixed inbound submission and modification latency. `--cancel-latency-ms` is fixed inbound
cancellation latency; its default equals `--latency-ms`. The two values are total operation delays, not additive
base and operation delays. A stale quote remains exposed while its cancellation is in transit. Examples:

```sh
lab fast backtest SYMBOL --strategy maker --latency-ms 150
lab fast backtest SYMBOL --strategy maker --latency-ms 150 --cancel-latency-ms 300
```

Synthetic regressions stress submission and cancellation at 50, 150 and 300 ms and verify that a trade through
a stale quote can fill it before a delayed cancellation. Real candidate runs at those settings are still
required; a synthetic fill is not a performance measurement.

Acknowledgments and venue events are instantaneous. There is no calibrated return-path delay, network jitter,
packet loss or home latency distribution. L2 queue position and consumed liquidity are NautilusTrader's
simulation, not an observation of our hypothetical order's actual exchange queue. Hidden liquidity, our market
impact and exchange-specific queue effects remain limitations. No new exchange execution client is used.
The descriptive `maker-study` still assumes instantaneous cancellations and optimistic queue cancellations;
it cannot replace the matching-engine execution tests.

API basis: NautilusTrader's [execution flow](https://nautilustrader.io/docs/latest/concepts/backtesting/execution-flow/),
[streaming batches](https://nautilustrader.io/docs/nightly/concepts/backtesting/apis-and-runs/) and
[behavioral models](https://nautilustrader.io/docs/latest/concepts/behavioral_models/). Those pages may describe
newer releases; regression behavior is checked against the project's pinned 2.0.0rc7 build.
