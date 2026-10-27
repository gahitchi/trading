# F1.2 candidate: read-only native evidence boundary

**Status: PROTOTYPE — not F1 qualification, not broker-connected, not
deployment approval.** Branch `chatgpt/f1-native-observation-20261027`.
Depends on the isolated `economic_ledger.py` journal. No execution path,
Nautilus account, risk gate, or persistent store is modified.

## Implemented contract

`src/lab/economic_observation.py` provides:

- `observe_native_fill(event, instrument=...)`: records the native trade ID,
  side, symbol, last price/quantity and **explicit USD commission** using
  native `as_decimal()` only. Missing commissions, unsupported currencies,
  binary floats, ambiguous identity and nonpositive prices/quantities fail.
- `observe_cash_account(account, portfolio, instruments=..., as_of_ns=...)`:
  reads **one explicitly USD-based CASH account**, its reported total USD
  balance and account-scoped `Portfolio.net_position` for **declared**
  instruments. Native MARGIN and wallet balances are **not** equated to
  trade-settled cash. If the installed Nautilus API cannot scope positions by
  account ID, refuse rather than aggregate unrelated account positions.
- `reconcile_native(ledger, snapshot, fills)`: independently audit the entire
  immutable journal, require exactly matching native trade IDs and event
  economics, and expose exact cash and per-instrument position differences.
  No dollar tolerance hides mismatches, and no mismatches are automatically
  converted into capital deposits, dividends or estimated financing.

This is a **read-only evidence layer**. It does not add, mutate, correct,
classify, or settle any entry in native accounts; it does not send orders or
authenticate to a broker. It does not assume that an absent native commission
equals zero. The cash-side ledger must already contain independently
classified capital/settlement events.

## Limits and remaining F1.2 work

1. Current tests exercise the native-interface observation contract using
   protocol doubles. A **real NautilusTrader 2.0.0rc7 BacktestEngine account,
   Cache and Portfolio fixture with actual `OrderFilled` events** is still
   required before this adapter is accepted as native integration.
2. A listed-instrument snapshot is **not** a whole-account inventory: an
   unlisted native holding can remain invisible. Account-wide enumeration,
   observed event sequence identity, quiescent/atomic collection and
   timestamp-based reconciliation are future required evidence.
3. A reported native account balance is not proof of settled broker cash.
   CASH simulation, reported CASH, calculated MARGIN and reported MARGIN
   require distinct independently tested semantics. This slice rejects
   MARGIN instead of manufacturing a cash adjustment or fake fill.
4. No authorization or financial controls depend on this module. Independent
   account state update mechanisms, nontrade settlement, precision policy,
   fees/rounding, correction/restart custody and F1.3–F1.5 parity are still
   blocked by the whole-repository gap register.
5. Neither an unkeyed journal hash chain nor native in-process observations
   independently authenticate data origins. Trusted custody remains an F0/F1
   acceptance requirement.

## Test gate

```bash
uv sync --frozen --python 3.13 --extra dev
uv run --no-sync pytest -q tests/test_economic_ledger.py tests/test_economic_observation.py
uvx --from ruff==0.17.3 ruff check src tests tools
uvx --from ruff==0.17.3 ruff format --check src tests tools
python3 tools/collab.py check
```

Keep all external broker and live-money controls unchanged and disabled
for this development-only slice.
