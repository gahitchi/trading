# Borrow availability and financing evidence

Historical borrow evidence now reaches the backtest execution agent through the normal
`lab backtest`, comparison and walk-forward paths. The short-borrow null test fails closed
until prices, fees and availability have a jointly qualified resampling model. It does not authorize a
broker order or establish a strategy's statistical edge.

Configure an operator-supplied history in `lab.toml`:

```toml
[borrow]
history = "data/borrow.csv" # relative to lab.toml; missing/malformed files fail the run
max_age_days = 3           # calendar days, including weekends
```

CSV columns: `ticker,as_of,available,apr,source`. `as_of` is an ISO date, `available`
is true/false, APR is an annual decimal (0.12 means 12%), and `source` names the
provider. Records must represent information available by that date's close, not a
later revision. Duplicate dates, absent fee quotes for available shares and placeholder
sources are rejected. The code validates structure; it cannot authenticate vendor claims.

- New or larger shorts need a nonfuture, sufficiently recent availability record and fee.
  Without one, an attempted long-to-short flip closes the long without opening a short;
  a larger short stays at its current size. Any denied short freezes all new exposure for
  that rebalance, so a long hedge cannot be opened alone. Covers and long reductions need no new inventory.
- Fees use the short quantities and marks at the previous close. That book accrues on each
  calendar day from that date (inclusive) to the next recorded close (exclusive). Each day's
  APR is looked up separately. Monday entries are not charged for the preceding weekend;
  Monday rate changes are not applied to Friday. This is an explicit daily approximation,
  not a reproduction of broker intraday billing. Accrual ends at the last equity snapshot.
- An unavailable-inventory record can still supply an observed fee for an existing loan.
  If any held-book fee is missing or stale, the entire backtest raises `BorrowUnavailable`;
  no partial metrics or assumed zero/cheap fees are returned. A recall does not trigger a
  modeled buy-in: forced recalls/liquidations remain unimplemented.
- `summary.json` includes the parsed-history/freshness SHA-256 identity and every charged
  day's ticker, source, observation date, rate, marked short value and fee. The config snapshot
  includes the history path. A hash binds the input; it does not prove authenticity.

Omitting `[borrow].history` preserves the explicitly labeled assumed uniform fee model for
exploratory comparisons. Such results remain unqualified for historical borrow availability.
Debit interest continues to use the configured assumed rate in either model. Real-broker
short eligibility, quantity-specific locates, execution-time inventory, intraday fees, recalls
and house margin are separate qualification work. `lab paper` does not consume this historical
file; its financing remains estimated and its broker still has to accept each short.

A named risk profile now retains all configured caps in research. `custom` retains the
legacy research convention of removing the per-name cap and drawdown breaker, while
preserving its configured gross/short/net limits. Research evidence must therefore always
be interpreted alongside the saved effective settings.
