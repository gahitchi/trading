"""Typed, broker-neutral contract for the first offline trading laboratory.

No code in lab.markets can create an authenticated exchange client. LIVE is a
reserved mode, explicitly rejected by the reference simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from re import fullmatch


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    FOREX = "forex"
    EQUITY = "equity"


class ContractKind(StrEnum):
    SPOT = "spot"
    LINEAR_DERIVATIVE = "linear_derivative"
    FOREX_CASH = "forex_cash"
    SHARE = "share"


class TradingMode(StrEnum):
    OFFLINE_SIM = "offline_sim"
    BROKER_DEMO = "broker_demo"
    LIVE = "live"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self == Side.BUY else -1


class UnsupportedMode(ValueError):
    pass


def decimal(value: Decimal | str | int) -> Decimal:
    """Require exact, finite decimal input; floats are intentionally rejected."""
    if isinstance(value, (bool, float)):
        raise TypeError("financial input must be Decimal, str, or int, not float")
    v = Decimal(value)
    if not v.is_finite():
        raise ValueError("nonfinite financial input")
    return v


def positive(value: Decimal, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class Instrument:
    venue: str
    symbol: str
    asset_class: AssetClass
    kind: ContractKind
    quote_currency: str
    tick_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    multiplier: Decimal = Decimal("1")
    initial_margin: Decimal = Decimal("0.5")
    maintenance_margin: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        if not fullmatch(r"[A-Z0-9]{2,16}", self.venue):
            raise ValueError("venue must be uppercase alphanumeric")
        if not fullmatch(r"[A-Z0-9_:/-]{2,80}", self.symbol):
            raise ValueError("symbol must be an uppercase instrument identifier")
        if not fullmatch(r"[A-Z]{3,8}", self.quote_currency):
            raise ValueError("quote currency must be an uppercase currency")
        for name in ("tick_size", "quantity_step", "min_quantity", "min_notional", "multiplier"):
            positive(decimal(getattr(self, name)), name)
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal")
        if self.min_quantity % self.quantity_step:
            raise ValueError("minimum quantity must align to quantity step")
        for name in ("initial_margin", "maintenance_margin"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not Decimal("0") < decimal(value) <= Decimal("1"):
                raise ValueError(f"{name} must be in (0, 1]")
        if self.maintenance_margin > self.initial_margin:
            raise ValueError("maintenance margin cannot exceed initial margin")
        valid = {
            AssetClass.CRYPTO: {ContractKind.SPOT, ContractKind.LINEAR_DERIVATIVE},
            AssetClass.FOREX: {ContractKind.FOREX_CASH},
            AssetClass.EQUITY: {ContractKind.SHARE},
        }
        if self.kind not in valid[self.asset_class]:
            raise ValueError("contract type and asset class mismatch")

    @property
    def instrument_id(self) -> str:
        return f"{self.venue}:{self.symbol}"

    def check_quantity(self, quantity: Decimal) -> None:
        quantity = decimal(quantity)
        if quantity < self.min_quantity or quantity % self.quantity_step:
            raise ValueError("quantity violates minimum or step")

    def check_price(self, price: Decimal) -> None:
        positive(decimal(price), "price")
        if price % self.tick_size:
            raise ValueError("price is not aligned to tick size")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """A capability is valid only for the specified environment + instrument."""

    venue: str
    mode: TradingMode
    supported_instruments: frozenset[str]
    may_short: bool
    may_leverage: bool
    may_submit_orders: bool

    def require(self, instrument: Instrument, mode: TradingMode, *, short: bool = False) -> None:
        if mode != self.mode or instrument.venue != self.venue:
            raise ValueError("capability belongs to a different venue/environment")
        if instrument.instrument_id not in self.supported_instruments:
            raise ValueError("instrument is not qualified for this venue/environment")
        if not self.may_submit_orders:
            raise ValueError("execution disabled in capability")
        if short and not self.may_short:
            raise ValueError("short selling not authorized")


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    exchange_ns: int
    received_ns: int
    price: Decimal
    quantity: Decimal
    taker_side: Side | None = None
    venue_source: str | None = None

    def __post_init__(self) -> None:
        if not self.trade_id or len(self.trade_id) > 128:
            raise ValueError("trade ID missing or too long")
        if not isinstance(self.exchange_ns, int) or not isinstance(self.received_ns, int):
            raise TypeError("timestamps must be Unix nanoseconds")
        if self.exchange_ns < 0 or self.received_ns < self.exchange_ns:
            raise ValueError("invalid exchange/receive timestamps")
        for name in ("price", "quantity"):
            v = getattr(self, name)
            if not isinstance(v, Decimal):
                raise TypeError(f"{name} must be Decimal")
            positive(decimal(v), name)
        if self.taker_side is not None and not isinstance(self.taker_side, Side):
            raise TypeError("taker_side must be a Side value")
        if self.venue_source is not None and (
            type(self.venue_source) is not str or not 1 <= len(self.venue_source) <= 16
        ):
            raise ValueError("invalid trade source label")


@dataclass(frozen=True, slots=True)
class MarkPrice:
    """Independent valuation/liquidation event; never inferred from a trade."""

    mark_id: str
    exchange_ns: int
    received_ns: int
    price: Decimal

    def __post_init__(self) -> None:
        if not self.mark_id or len(self.mark_id) > 128:
            raise ValueError("mark ID missing or too long")
        if type(self.exchange_ns) is not int or type(self.received_ns) is not int:
            raise TypeError("mark timestamps must be integer nanoseconds")
        if self.exchange_ns < 0 or self.received_ns < self.exchange_ns:
            raise ValueError("invalid mark timestamps")
        if not isinstance(self.price, Decimal):
            raise TypeError("mark price must be Decimal")
        positive(decimal(self.price), "mark price")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    instrument_id: str
    side: Side
    quantity: Decimal
    created_ns: int
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if not fullmatch(r"[A-Za-z0-9]{1,32}", self.client_order_id):
            raise ValueError("client order ID must be 1-32 alphanumerics")
        if not self.instrument_id or len(self.instrument_id) > 100:
            raise ValueError("invalid instrument ID")
        if not isinstance(self.quantity, Decimal):
            raise TypeError("quantity must be Decimal")
        positive(decimal(self.quantity), "quantity")
        if not isinstance(self.created_ns, int) or self.created_ns < 0:
            raise ValueError("created_ns must be nonnegative int")
        if not isinstance(self.reduce_only, bool):
            raise TypeError("reduce_only must be bool")
