"""F1.2 read-only native observations and exact comparison, NOT broker integration.

Only a dedicated, USD-base Nautilus CASH account is accepted. Margin balances
cannot be called cash without a separately reviewed settlement model. The
listed-instrument observation does not prove whole-account coverage.
No trading, account mutation, authority, or implicit source classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from lab.economic_ledger import EconomicLedger, EconomicLedgerError, exact

USD = "USD"


class ObservationError(ValueError):
    """Native evidence is incomplete, imprecise, unsupported or inconsistent."""


def _decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal:
        raise ObservationError(f"{field} must be a native Decimal, not a float or string")
    try:
        return exact(value)
    except EconomicLedgerError as exc:
        raise ObservationError(f"{field} is not exactly representable") from exc


def _money(value: object, field: str) -> Decimal:
    if getattr(getattr(value, "currency", None), "code", None) != USD:
        raise ObservationError(f"{field} must be denominated in USD")
    method = getattr(value, "as_decimal", None)
    if not callable(method):
        raise ObservationError(f"{field} requires native Money.as_decimal()")
    return _decimal(method(), field)


def _quantity(value: object, field: str) -> Decimal:
    method = getattr(value, "as_decimal", None)
    if not callable(method):
        raise ObservationError(f"{field} requires native decimal quantity/price")
    return _decimal(method(), field)


@dataclass(frozen=True, slots=True)
class NativeFillObservation:
    trade_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal


@dataclass(frozen=True, slots=True)
class NativePositionObservation:
    symbol: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class NativeCashObservation:
    account_id: str
    as_of_ns: int
    cash: Decimal
    positions: tuple[NativePositionObservation, ...]
    scope: str = "DECLARED_USD_EQUITIES_ONLY"


@dataclass(frozen=True, slots=True)
class NativeParity:
    cash_delta: Decimal
    position_deltas: tuple[tuple[str, Decimal], ...]
    fill_ids_checked: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return self.cash_delta == 0 and all(delta == 0 for _, delta in self.position_deltas)


def observe_native_fill(event: object, *, instrument: object) -> NativeFillObservation:
    """Extract broker/simulator OrderFilled bytes without float or fee defaults."""
    if getattr(event, "instrument_id", None) != getattr(instrument, "id", None):
        raise ObservationError("fill instrument and supplied instrument disagree")
    if getattr(getattr(instrument, "quote_currency", None), "code", None) != USD:
        raise ObservationError("only USD-quoted instruments are supported")
    side = getattr(getattr(event, "order_side", None), "name", None)
    if side not in ("BUY", "SELL"):
        raise ObservationError("unsupported native order side")
    trade_id = str(getattr(event, "trade_id", ""))
    symbol = str(getattr(getattr(instrument, "id", None), "symbol", ""))
    if not trade_id or trade_id == "None" or not symbol or symbol == "None":
        raise ObservationError("missing native fill identity")
    qty = _quantity(getattr(event, "last_qty", None), "fill quantity")
    price = _quantity(getattr(event, "last_px", None), "fill price")
    fee = _money(getattr(event, "commission", None), "fill commission")
    if qty <= 0 or price <= 0 or fee < 0:
        raise ObservationError("native fill quantity and price must be positive, fee nonnegative")
    return NativeFillObservation(trade_id, symbol, side, qty, price, fee)


def observe_cash_account(
    account: object,
    portfolio: object,
    instruments: Mapping[str, object],
    *,
    as_of_ns: int,
) -> NativeCashObservation:
    """Read selected native account and account-scoped positions; never write.

    Requires a dedicated USD CASH account and a caller-provided complete
    instrument set for the desired scope. No margin or account-wide assertion.
    """
    if type(as_of_ns) is not int or as_of_ns <= 0:
        raise ObservationError("observation timestamp must be positive integer nanoseconds")
    if getattr(getattr(account, "account_type", None), "name", None) != "CASH":
        raise ObservationError("margin/wallet/account-type cash inference is unqualified")
    if getattr(getattr(account, "base_currency", None), "code", None) != USD:
        raise ObservationError("account must have an explicit USD base currency")
    account_id = getattr(account, "id", None)
    if account_id is None or str(account_id) in ("", "None"):
        raise ObservationError("native account identity is missing")
    balance = getattr(account, "balance_total", None)
    if not callable(balance):
        raise ObservationError("native account has no total-balance observation")
    cash = _money(balance(), "native account balance")
    if not isinstance(instruments, Mapping) or not instruments:
        raise ObservationError("explicit declared instrument scope required")
    positions: list[NativePositionObservation] = []
    net_position = getattr(portfolio, "net_position", None)
    if not callable(net_position):
        raise ObservationError("native account-scoped portfolio reader unavailable")
    for symbol, instrument_id in sorted(instruments.items()):
        if type(symbol) is not str or not symbol or str(getattr(instrument_id, "symbol", "")) != symbol:
            raise ObservationError("invalid instrument identity/scope")
        try:
            qty = net_position(instrument_id, account_id=account_id)
        except (TypeError, AttributeError) as exc:
            raise ObservationError("native portfolio does not support account-scoped positions") from exc
        positions.append(NativePositionObservation(symbol, _decimal(qty, "native net position")))
    return NativeCashObservation(str(account_id), as_of_ns, cash, tuple(positions))


def reconcile_native(
    ledger: EconomicLedger,
    snapshot: NativeCashObservation,
    fills: tuple[NativeFillObservation, ...],
) -> NativeParity:
    """Compare independent ledger business events with read-only native evidence.

    A match is NOT evidence of unseen instruments, broker postings, source
    authenticity, atomic capture, external cash classification or F1 approval.
    """
    if type(ledger) is not EconomicLedger or type(snapshot) is not NativeCashObservation:
        raise ObservationError("typed ledger and native snapshot required")
    if type(fills) is not tuple or any(type(f) is not NativeFillObservation for f in fills):
        raise ObservationError("typed immutable native fill observations required")
    if (
        snapshot.scope != "DECLARED_USD_EQUITIES_ONLY"
        or type(snapshot.account_id) is not str
        or not snapshot.account_id
        or type(snapshot.as_of_ns) is not int
        or snapshot.as_of_ns <= 0
    ):
        raise ObservationError("unsupported account or observation scope")
    ledger.audit()
    expected = {event.event_id: dict(event.details) for event in ledger.journal if event.kind == "FILL"}
    native = {f.trade_id: f for f in fills}
    if len(native) != len(fills) or set(native) != set(expected):
        raise ObservationError("native and journal trade IDs do not match exactly")
    for trade_id, fill in native.items():
        row = expected[trade_id]
        if (
            (row.get("symbol"), row.get("side")) != (fill.symbol, fill.side)
            or _decimal(fill.quantity, "native fill quantity") != _decimal(
                Decimal(row["quantity"]), "ledger fill quantity"
            )
            or _decimal(fill.price, "native fill price") != _decimal(Decimal(row["price"]), "ledger fill price")
            or _decimal(fill.commission, "native commission") != _decimal(
                Decimal(row["commission"]), "ledger commission"
            )
        ):
            raise ObservationError("native fill does not match ledger business event")
    if type(snapshot.positions) is not tuple or any(
        type(p) is not NativePositionObservation for p in snapshot.positions
    ):
        raise ObservationError("typed position observations required")
    observed = {p.symbol: _decimal(p.quantity, "native position") for p in snapshot.positions}
    if len(observed) != len(snapshot.positions):
        raise ObservationError("duplicate native instrument observation")
    expected_symbols = {lot.symbol for lot in ledger.lots}
    # Both sides must explicitly cover every position; flat declared symbols
    # remain valid observations, but hidden ledger instruments are refused.
    if expected_symbols - observed.keys():
        raise ObservationError("native observations omit an open ledger instrument")
    deltas = tuple(
        (symbol, observed[symbol] - ledger.position(symbol)) for symbol in sorted(observed)
    )
    cash_delta = _decimal(snapshot.cash, "native USD cash") - ledger.balance("cash:USD")
    return NativeParity(cash_delta, deltas, tuple(sorted(native)))
