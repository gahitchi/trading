# Funding-uncertainty daily equity envelope — research-only v1 (2026-11-10)

**Status: new code and 11 synthetic tests awaiting Termux acceptance.**
No new strategy evaluation, market data import, network request, holdout
opening, paper/live trading authorization, or research-family mutation.

## Motivation

The earlier `funded_daily_short_equity` refuses any modeled short
whose minute-resolution entry/exit could overlap a funding settlement.
Dropping such trades from a performance sample would bias the result.
`src/lab/bearish_funding_interval_equity.py` therefore maintains
**two complete accounting paths** over **all** the modeled trades
instead of removing ambiguous positions.

The new `interval_daily_short_equity` first invokes the strict
`daily_short_equity` validation and `short_funding_bounds` with a
frozen, externally specified **expected source-plan SHA-256**. The
existing source hashes, UTC timestamps, complete funding timeline,
protected boundary, OHLC evidence and trade fill-cost reconciliation
must all validate before any accounting.

## Ledger rules

- A short's quantity is calculated from the **scenario's realized cash
  at entry**: `qty = cash × notional_fraction / entry_reference`.
  Each endpoint scenario reinvests independently and carries no
  unrealized gains into entry sizing.
- Entry and exit are posted at the modeled minute timestamps; fees,
  adverse spread and slippage are preserved. The replay's fixed
  funding reserve is **removed**, not added to funding events.
- The existing audited minute-fill funding ownership test classifies
  each exchange event as **certain**, **ambiguous**, or outside the
  position's possible lifetime. Certain funding is booked in both
  paths. Ambiguous **negative** funding is charged in the lower path;
  ambiguous **positive** funding is credited only to the upper path.
  Zero-funding events do not change cash.
- Funding is posted at its recorded timestamp, or at the trade's
  modeled opening timestamp if the event was within the ambiguous
  window immediately **before** the entry. An event possibly owned
  by a trade but stamped just **after** its modeled exit is not
  posted before that later timestamp. Same-instant exits precede
  settlements, and a coincident entry precedes its own ambiguous
  funding. Entries exactly at 00:00 UTC are deferred to the next
  reporting day.
- Each UTC day includes its start equity, ending hypothetical marked
  equity, scenario net P&L, funding cashflow and open-inventory flag.
  Flat days remain present. Open shorts are marked with a hypothetical
  adverse full close-out; this is not an executable order-book price.
- The source-bound event counts and total per-trade funding are
  reconciled against the existing `short_funding_bounds` outputs.
  Both paths must be solvent under a conservative lifetime-high
  stress check and consistent at every daily mark; otherwise the
  entire report is **unavailable**, not filtered or sanitized.

**Interpretation:** These are intentionally wide, conservative
**outer scenarios**, not a probability distribution or exact
tight endpoints of all joint market/execution states. An exchange
settlement near two adjacent modeled trades may contribute to
both trades' outer bounds although only one ownership may be
physically realized. This is pessimistic/optimistic coverage, not a
claim that both events were credited in a real broker wallet.
Scenario daily P&L deltas are **not** standalone independent
minimum/maximum daily P&L estimates. Historical replay outcomes,
uncertainty bands and mark-to-market equity remain
`qualified=false`.

## Acceptance

```sh
cd ~/trading-journal-validation
source ~/trading-venv/bin/activate
git fetch origin
git switch --detach origin/chatgpt/codex-maintenance
ruff check src tests
ruff format --check src tests
PYTHONPATH=src:. python -m pytest \
  tests/test_bearish_ensemble.py \
  tests/test_bearish_ensemble_screen.py \
  tests/test_bearish_ensemble_replay.py \
  tests/test_bearish_ensemble_research.py \
  tests/test_bearish_equity.py \
  tests/test_bearish_funding_audit.py \
  tests/test_bearish_margin_audit.py \
  tests/test_bearish_funded_equity.py \
  tests/test_bearish_funding_interval_equity.py -q
```

11 new synthetic cases exercise flat days, certain funding parity
with the strict ledger, ambiguous debits and receipts, exact
exit boundaries, a provably post-exit event, overnight UTC
settlements, scenario-dependent reinvestment, source-tampering
detection, holdout/gap enforcement, unresolved or modified
positions and funding-free shorts. All tests use constructed
data; they do **not** demonstrate strategy profitability.

## Remaining qualification work

Authenticate venue-specific mark-price and liquidation evidence;
reconcile real paper-wallet settlement and execution records;
add dependency-aware simultaneous-event ownership bounds;
model order-book spreads/impact, partial fills and latency; and
preregister independent short-strategy evaluations. Do not rerun
the retired BTC ensemble v1 trial to exercise this model.
