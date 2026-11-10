# Research-only funding bounds and margin envelope (2026-11-10)

**Status: code and synthetic tests only. No historical profitability or execution
qualification. The retired BTCUSDT Bearish Ensemble v1 is unchanged.**

## 1. Source-pinned funding bounds

`src/lab/bearish_funding_audit.py` adds `short_funding_bounds`, a pure
adapter for a previously produced `ShortReplay`. It does **not** read
parquet files, call an exchange, register experiments or touch a holdout.
It accepts only a `crash_funding.load_verified`-style source snapshot
whose **plan SHA-256 must match an independently supplied frozen
registration digest**. It rechecks the exact plan structure, event
digest and count, symbol, UTC funding timestamps, ordered events,
pre-holdout boundary and entire source timeline coverage. Source hashes
are integrity evidence, not cryptographic proof of exchange origin.

The existing `crash_funding_ledger.funding_bounds` policy models
uncertain ownership around 1-minute fills. A positive funding rate
normally credits the short, and a negative one debits it. It reports
certain settlements and the conservative lower / possible upper return
for ambiguous settlements. It rejects missing source coverage instead
of silently assuming zero funding.

The original minute-bar replay already charged a **fixed adverse
funding reserve**. To avoid double-counting, each scenario uses

```
net_before_actual_funding = original_net + original_funding_reserve_cost
lower_net = net_before_actual_funding + funding_lower
upper_net = net_before_actual_funding + funding_upper
```

The report contains every trade and sum of modeled return fractions.
The sums are **not equity, account profit, or daily P&L**. This stage
does not post settlement cash to an equity ledger or make claims about
actual Binance account funding.

**Required evidence gate:** load the original source with
`crash_funding.load_verified(config, funding_plan, symbol="BTCUSDT")`
and pass its separately registered `funding_plan_sha256` as
`expected_plan_sha256`. New data sources and any new evaluation must
be frozen in their own research-family plan first. Do **not** re-run
the retired v1 strategy to use this adapter.

## 2. Conservative maintenance-margin envelope

`src/lab/bearish_margin_audit.py` adds `margin_envelope`: an independent
read-only diagnostic over existing trade records and the same fully
closed, UTC-minute-indexed candle frame as `daily_short_equity`.
It enforces the existing source/candle/holdout/account-conservation
checks first, then computes a short's **worst observed candle high**
during its lifetime and a hypothetical buy-to-cover cost.

The stress equity subtracts modeled spread/slippage, commissions and
the full fixed adverse funding reserve. From that stressed equity, it
subtracts an illustrative maintenance requirement of
`position quantity × worst high × (maintenance_fraction + buffer_bps/10000)`.

Default examples: **10% initial margin**, **5% maintenance margin**,
**10-bps buffer**. These are NOT authenticated Binance maintenance
brackets and never imply that any given leverage is allowed. A
maintenance breach or initial-margin shortfall raises an error, rather
than inserting a fictional liquidation or selecting away the losing
trade. The stress is retrospective: the future bar high is not an
entry-time forecast or tradable risk decision.

No funding-adjusted equity or cross-margin result should be claimed:
the two adapters are deliberately separate from the registered
`bearish_ensemble_research` implementation, and they are not called
from its historical `--evaluate` command.

## Synthetic-only acceptance

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest \
  tests/test_bearish_funding_audit.py \
  tests/test_bearish_margin_audit.py -q
```

The 20 added synthetic tests cover known positive/negative settlement
cashflows, ambiguous edge ownership, fixed-reserve substitution,
source/timeline hashes, unordered or missing events, holdout boundaries,
tampered fills, maintenance breaches, safe positions, and inherited
daily equity input checks.

## Remaining before any strategy qualification

- Authenticated, time-aligned exchange *mark prices* and order-book/
  trade execution evidence (spread, impact, queue, rejections, fills).
- Genuine event-time settlement posting into daily cash/equity with
  source completeness, no future information, and explicit uncertainty.
- Exchange-specific **position-tier** initial/maintenance margin,
  liquidation dynamics, fees, funding/borrow and portfolio collateral.
- Newly preregistered candidate hypotheses, independent data,
  defensible paper-trading risk controls, and dependence-adjusted
  inference. Do not use this accounting work as evidence of an edge.
