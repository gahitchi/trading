# Research-only daily equity from modeled shorts (2026-11-10)

**Development status: synthetic accounting checks, not historical qualification.**

`src/lab/bearish_equity.py` implements a separate, deterministic daily
marked-equity calculation for an existing `ShortReplay`. It does NOT alter
`bearish_ensemble_research.py`, the frozen BTC signal configuration or any
existing research registration. **Do not replay or re-register retired
BTC v1 for this feature.**

## Economic accounting convention

- Initial reference equity: **10,000 USDT**, fixed nonleveraged gross
  notional equal to **25% of realized equity at each new entry**. The
  fraction may be explicitly changed only within (0, 1]; there is no
  exchange-specific leverage or liquidation engine. A 10%-of-notional
  minimum worst-high equity floor is a conservative research veto, not a
  forecast of actual maintenance margin.
- Each modeled trade uses a fixed base quantity
  `(equity_before_entry × notional_fraction) / entry_reference`.
  Sequential positions size off *realized* equity, not unrealized mark
  gains. There are no deposits, withdrawals or overlapping shorts.
- Realized P&L uses the replay's executed short entry/cover reference
  prices, adverse spread/slippage, two taker commissions and adverse
  funding **reserve estimate**. The ledger independently reconciles
  these fields before accepting a trade. Actual funding settlement,
  rate direction, borrow fees, insurance and exchange liquidations
  are **not observed** by this module.
- An open short at the UTC close is valued at the candle close with an
  extra **hypothetical adverse buy-to-cover spread and slippage**, both
  commissions and the FULL adverse fixed funding reserve. Charging
  the complete reserve while open avoids using the future exit duration
  in earlier day valuations. This is a hypothetical close-out valuation, not an
  executable bid/ask price or authenticated mark source.
- The first bar ends at **00:01 UTC** and every successive minute must be
  present until the last **00:00 UTC** candle-end stamp. Each day is
  recorded at that end stamp with the label of the immediately preceding
  UTC date. An explicit **UTC-midnight cutoff is mandatory**; the frame
  must contain no observations at or beyond that protected boundary. Intraday price gaps, invalid candles, partial UTC days,
  out-of-window entries/exits, unresolved positions and nonpositive
  account equity **stop the calculation**. Do not fill gaps, skip
  losing days, or fabricate a terminal close.
- Output includes every closed UTC day, including no-trade days, with
  start/end equity, net P&L, return, realized cash, unrealized closeout
  value, entry/exit counts, and an open-position indicator. It also
  reports cumulative **model** return and model daily-close drawdown,
  but always `qualified=false`.

## Acceptance and next evidence gates

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest tests/test_bearish_equity.py -q
```

Ten isolated synthetic scenarios exercise flat days, overnight marks,
sequential reinvestment, incomplete inputs, tampered fills/costs,
untrusted active position data, overlap, adverse high price spikes and
terminal inactivity, and protected-holdout rejection. Passing these tests shows conformance to **the
model rules**, not financial profitability.

A future independent research registration must specify data coverage,
actual funding payments, fee tiers, mark-price sources, maintenance
margin/liquidation rules, position sizing, order execution and an
exposure-matched baseline. Missing archive minutes or partial final days
are not silently made eligible by this module. New data qualification
and independent validation are mandatory before any paper/live promotion.
