# F1 — independent corporate-action cash settlement protocol

**Status:** reference financial-accounting stage implemented on
`chatgpt/corporate-action-ledger-20261026`; **F1 remains blocked** pending
proof that pinned NautilusTrader 2.0.0rc7 Portfolio, Cache, and Account state
follow the same retirement/settlement without fabricated exchange fills.
Do not merge into a trade-authorizing path without that proof.

## Accounting and temporal contract

`lab.markets.corporate_actions` implements immutable, exact-Decimal:
- `CashTermination`: action ID, instrument/currency, `cash_merger` or final
  `bankruptcy_writeoff`, **zero or positive** consideration, economic
  effective timestamp, vendor-available timestamp, earliest payable timestamp,
  versioned source and SHA-256 evidence reference.
- `ReconciledBook`: asserted verified position, outstanding GTC/order-state
  evidence, and action version. This is **not authenticated by the reference
  ledger**; a production native/broker adapter must independently prove it.
- `CorporateBook`: actual cash, held quantity, signed historic cost basis,
  booked realized PnL and separately signed **receivable/payable**. The
  `pending_cash` balance is **not** spendable cash.
- `CashReceipt`: independently identified, provenance-bound signed bank
  transfer, availability and audit digest. Partial payments are allowed; a
  duplicate ID with matching bytes has exactly-once effect, while a conflicting
  duplicate or overpayment is rejected.

Two **different** operations are required:

1. `recognize()` is permitted only after the effective and actual source
   **availability** times, a matching instrument/currency/action version,
   a verified held quantity and independently reconciled zero open orders.
   Pending cancel is still open. It retires the position and its signed
   historical basis into booked PnL, accrues a signed cash
   receivable/payable, and **does not transfer cash or execute a fill**.
   Fractional cash consideration requiring broker cash-in-lieu rounding is
   rejected instead of guessed.
2. `receive()` requires an actual source-identified receipt/debit,
   observed at or after availability and the earliest documented settlement
   date. It changes cash and offsets the pending receivable/payable, without
   reopening exposure or modifying realized PnL. Exact repeats have no second
   effect. Negative amounts must match a short liability; positive payments
   cannot discharge a short debt.

`lab.markets.corporate_journal.CorporateSession` binds the **original**
book's complete canonical state to `EventJournal`'s existing SQLite
append-only SHA-256 chain. It persists corporate-action recognition and cash
payment as different kinds, with a digest of the **resulting** independent
book. Journal commit occurs before in-memory acknowledgement; replay must
re-execute transitions and verify every state hash. Replay rejects ordinary
fills, unknown kinds and a changed starting book. The hash chain detects
accidental and ordinary unprivileged tampering, not a privileged attacker who
can rewrite the entire database and all provenance files.

### Conservation examples — USD

| Holding before action | Signed historical cost | Cash consideration | Due, not yet paid | Realized PnL | Actual cash at recognition |
| --- | ---: | ---: | ---: | ---: | ---: |
| Long 10 shares | +400.00 | 50.00/share | +500.00 | +100.00 | unchanged |
| Short 10 shares | −400.00 | 50.00/share | −500.00 | −100.00 | unchanged |
| Bankruptcy, long 10 | +400.00 | 0.00 | 0.00 | −400.00 | unchanged |
| Final zero recovery, short 10 | −400.00 | 0.00 | 0.00 | +400.00 | unchanged |

These are **independent equity-style, cost-basis accounting identities**,
not direct broker cash-account snapshots. Short borrow expenses, stock-loan
recalls, dividends, corporate tax withholding, fraction handling, rights,
liquidation waterfalls, cross-currency conversion, fees and venue-specific
requirements are excluded and must not be silently folded into this model.

## Explicit non-qualification of the native engine

NautilusTrader's position/event documentation distinguishes fill-driven
position closure, `PositionAdjusted`, and `AccountState`. Its existing
non-fill contract-expiration settlement is documented **only for binary-option
positions in a live node**, while its backtest/sandbox expiration path still
uses matching-engine fills. There is no verified general equities
cash-merger/bankruptcy position/cash adjustment API in the pinned 2.0.0rc7
integration at this checkpoint; generalizing the binary-option facility
would be unsafe. Sources:
- https://nautilustrader.io/docs/latest/concepts/positions/
- https://nautilustrader.io/docs/latest/concepts/events/
- https://nautilustrader.io/docs/latest/concepts/backtesting/execution-flow/
- https://github.com/nautechsystems/nautilus_trader/discussions/4829

**Next native proof:** probe the pinned release on a supported CI/Arch runtime,
identify a genuinely supported account/position transition (or design and
review an upstream adapter), and instrument it with exact native
portfolio/cache/account readbacks. A real release needs assertions that the
retired instrument is absent from the native live book, source-account
currency movements match the independent cash receipt, pending obligations
have custody evidence, and crash recovery cannot replay a receipt twice.
If the pinned engine cannot express these semantics, keep the backtest
fail-closed. Do not inject arbitrary Python `ExecutionAgent.cash`, synthetic
`OrderFilled` at a one-tick price, fictitious post-delisting bars/volume,
or ignore independent ledger versus native-account mismatches.

## Device and CI test instructions (no credentials)

In Termux's existing checkout, no NumPy or Nautilus installs are needed for
this stage:

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/corporate-action-ledger-20261026
git pull --ff-only
git rev-parse HEAD
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
```

The portable tests cover long/short accrual, zero and positive recoveries,
delayed/partial receipts, GTC/version uncertainty, precision, duplicates,
absence of lookahead, whole-journal restart, tamper and full deterministic
replay, including Python `-S` with no site-packages. No broker account is
connected and no money can move.

**Gate effect:** F1 gets a separately tested independent corporate-action
financial-accounting foundation. F1 stays **blocked**. F3 and F4 retain
their separate broker/late-fill/risk and authenticated state-machine gates;
all seven external release criteria remain independently unqualified.
