"""Read-only NautilusTrader settlement parity *necessary-condition* gate.

Corporate-action accounting is a separate cash/position lifecycle, never a
market order. This module does NOT provide a native account/position mutation
API and cannot authorize delisting backtests or broker operations. It checks
actual Nautilus Portfolio/Cache/Account state against the independent Decimal
book and rejects mismatches. A passing gate would remain insufficient without
an independently approved native corporate-action transition and broker data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.model import Currency, InstrumentId, Venue

from lab.markets.corporate_actions import CorporateBook


class NativeCorporateParityError(ValueError):
    """Native Nautilus account, portfolio, or cache contradicts financial ledger."""


@dataclass(frozen=True, slots=True)
class NativeCorporateReadback:
    instrument: str
    venue: str
    currency: str
    position_flat: bool
    cache_open_positions: int
    cache_open_orders: int
    account_total: Decimal
    fill_count: int
    native_engine_readback: bool = True
    native_corporate_action_transition_verified: bool = False
    external_broker_reconciliation_verified: bool = False


def read_native_corporate_state(
    engine, *, instrument_id: InstrumentId, venue: Venue, currency: Currency
) -> NativeCorporateReadback:
    """Inspect the actual pinned native backtest state without changing it.

    The cache exposes order/position histories even when a top-level
    portfolio flat flag says otherwise; check both to avoid cached ghosts.
    Return no user-supplied 'verified' booleans as a substitute for observations.
    """
    if type(instrument_id) is not InstrumentId or type(venue) is not Venue or type(currency) is not Currency:
        raise NativeCorporateParityError("pinned typed native instrument/venue/currency required")
    native_instrument = engine.cache.instrument(instrument_id)
    if native_instrument is None:
        raise NativeCorporateParityError("native instrument is absent from cache")
    if native_instrument.id.venue != venue:
        raise NativeCorporateParityError("native instrument venue differs from account venue")
    account = engine.cache.account_for_venue(venue)
    if account is None:
        raise NativeCorporateParityError("native venue has no cached account")
    balance = account.balance_total(currency)
    if balance is None:
        raise NativeCorporateParityError("native account lacks settlement-currency total")
    open_positions = engine.cache.positions_open(instrument_id=instrument_id)
    open_orders = engine.cache.orders_open(instrument_id=instrument_id)
    reports = engine.generate_fills_report()
    value = balance.as_decimal()
    if type(value) is not Decimal or not value.is_finite():
        raise NativeCorporateParityError("native cash readback is nonfinite or not exact Decimal")
    return NativeCorporateReadback(
        instrument=str(instrument_id),
        venue=str(venue),
        currency=currency.code,
        position_flat=bool(engine.portfolio.is_net_flat(instrument_id)),
        cache_open_positions=len(open_positions),
        cache_open_orders=len(open_orders),
        account_total=value,
        fill_count=len(reports),
    )


def require_native_settlement_parity(
    book: CorporateBook,
    readback: NativeCorporateReadback,
    *,
    baseline_fill_count: int,
) -> None:
    """Refuse any mismatch; do not claim that passing establishes legality.

    Native position closure, clearing all open orders, and unchanged exchange
    fills must ALL hold. Any cash movement must match the independently
    recorded *actual* receipt/debit, not the unpaid receivable.
    """
    if type(book) is not CorporateBook or type(readback) is not NativeCorporateReadback:
        raise NativeCorporateParityError("independent typed ledger and native readback required")
    if book.action_id is None:
        raise NativeCorporateParityError("no recognized corporate cash action")
    if book.position or book.signed_basis:
        raise NativeCorporateParityError("independent ledger still holds instrument")
    if book.instrument_id != readback.instrument or book.currency != readback.currency:
        raise NativeCorporateParityError("native instrument/currency differ from independent ledger")
    if not readback.native_engine_readback:
        raise NativeCorporateParityError("not observed through the native engine")
    if not readback.position_flat or readback.cache_open_positions:
        raise NativeCorporateParityError("Nautilus Portfolio/Cache still contains an open corporate-action holding")
    if readback.cache_open_orders:
        raise NativeCorporateParityError("open native orders remain, including pending cancel")
    if type(baseline_fill_count) is not int or baseline_fill_count < 0:
        raise NativeCorporateParityError("invalid baseline native fill count")
    if readback.fill_count != baseline_fill_count:
        raise NativeCorporateParityError("new native market fills appeared during non-trading cash settlement")
    if readback.account_total != book.cash:
        raise NativeCorporateParityError("native account cash differs from independently observed paid cash")
    # Deliberately NO return of an 'approved' / 'qualified' result. The reader
    # only proves necessary observable invariants, not that the native
    # transition or broker-sourced cash receipt was genuine.
