# F1 pinned Nautilus readback — native settlement necessary-condition test

**Status:** A diagnostic guard and real Nautilus synthetic regression, not an
implemented corporate-action close and **not** F1 approval.

## What changed

- `src/lab/markets/native_corporate_parity.py` reads the **actual**
  pinned `BacktestEngine.cache` and `BacktestEngine.portfolio`, including:
  cached instrument/venue identity; the account's `balance_total(currency)`;
  `portfolio.is_net_flat(instrument_id)`; `cache.positions_open` and
  `cache.orders_open` for that instrument; and the engine's native fill report.
  It does **not** change any of them.
- `require_native_settlement_parity` treats each as a **necessary** condition:
  the independent ledger's action must be recognized and flat; the native
  portfolio and open-position cache must **also** be flat, all native orders
  closed, the number of native exchange fills unchanged from before the
  corporate action, and the native account's actual total cash identical to
  independent **paid** cash (never accrued `pending_cash`).
- `tests/test_native_corporate_parity.py` runs actual pinned engine positions
  with **synthetic** fixed bid/ask data. For LONG and SHORT, it buys/sells via
  one bona fide native fill, recognizes a bankruptcy write-off in the independent
  ledger **only**, then requires an explicit mismatch rejection because Nautilus
  still holds the position. Another test performs an **ordinary native
  buy/sell round trip** and requires rejection if its exit fill is relabeled
  as a non-trading corporate settlement.

This directly exposes the compatibility trap: a Python-only cash/position
write-off would make external financial records flat while the native engine
still holds a trading position. Inventing a sell/buy at a $0.0001 price
would make native state flat but violate the unchanged-native-fill condition
and create false market liquidity.

## Important scope boundaries

The reader has no market data, broker, API keys, order submission or account
mutation features. It returns typed observations and deliberately has **no
approval bit**. Even a passing set of necessary conditions cannot prove that
a non-fill close was supported by the engine, that the broker paid the
receipt, or that positions and account balances survive restart.

Nautilus' current documented `PositionAdjusted` behavior covers
non-ordinary changes such as spot base-currency commissions and perpetual
funding; the ordinary net-position cycle is still fill-driven. It is
**unsafe to assume** the availability of a generic equity cash-merger
position-close API from the event class name alone. Official docs:
- https://nautilustrader.io/docs/latest/concepts/positions/
- https://nautilustrader.io/docs/latest/concepts/events/
- https://nautilustrader.io/docs/latest/concepts/execution/
- https://nautilustrader.io/docs/latest/concepts/backtesting/accounts-and-margin/

The `latest` docs may evolve; the independently run integration tests use
the **pinned** project's NautilusTrader dependency.

### Native acceptance on a supported Linux runner

```sh
uv sync --frozen --extra dev
uv run --no-sync pytest -q tests/test_native_corporate_parity.py
uv run --no-sync pytest -q tests/test_portable_corporate_actions.py tests/test_portable_corporate_journal.py
uvx ruff check src tests tools
uvx ruff format --check src tests tools
python3 tools/collab.py check
```

An expected **negative parity result** is success: the test passes only when
the guard rejects independent-only retirement and market-fill masquerading.
Termux native Android does not have the required pinned Linux/glibc engine.
Use its 46-test stdlib suite separately, without interpreting it as native
parity.

## What must be implemented to unblock F1

1. Identify and review an explicit non-market Nautilus-native account and
   position retirement transition in **the pinned release**, or build and
   independently audit the upstream integration that supplies that transition.
2. In the same event-clock scenario, close a genuine native holding without
   consuming invented exchange quotes/volume and without adding a normal
   market fill. Preserve order lifecycle and independent event provenance.
3. Reconcile exact native account cash with each evidenced partial payment
   or short debit. Hold an explicit receivable/payable outside spendable cash
   until cash actually settles.
4. Persist/cross-check native and independent state across crash/restart,
   pending GTC/partial-fill/cancel races, split version changes, short borrow
   obligations, zero-recovery, and any unsupported fractional entitlement.
5. Keep `backtest.settle_delistings` fail-closed until full, independently
   reviewed native account/cache/portfolio proof exists. This diagnostic
   cannot be substituted into a live or paper account to waive gate F1.

**Gate effects:** stronger native test coverage and improved observability,
but F1=BLOCKED, F6=BLOCKED; no external release criterion cleared.
