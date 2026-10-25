"""NautilusTrader crypto instrument registration boundary (OFFLINE_ONLY).

This is an actual Nautilus CryptoPerpetual model object, not a parallel
instrument implementation. The metadata is provided explicitly and is not
treated as authenticated exchange contract, account, fee or margin evidence.

Only *linear, quote-settled* crypto swaps are supported. In particular,
a provider's unverified 'BTC-USDT-SWAP' label does not qualify an EEA
X-Perps execution instrument. No adapter client or trading node is created.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.model import CryptoPerpetual, Currency, Money, Price, Quantity, Symbol

from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, AssetClass, ContractKind, Instrument, decimal
from lab.markets.nautilus_data import nautilus_instrument_id


def _precision(step: Decimal) -> int:
    decimals = max(0, -step.as_tuple().exponent)
    # Do not round a contract increment to fit Nautilus fixed precision.
    if decimals > 9:
        raise ValueError("increment precision exceeds qualified Nautilus fixed precision")
    return decimals


@dataclass(frozen=True, slots=True)
class LinearContractDefinition:
    instrument: Instrument
    base_currency: str
    settlement_currency: str
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    definition_ns: int
    observed_ns: int
    metadata_source: str

    def __post_init__(self) -> None:
        c = self.instrument
        if c.asset_class != AssetClass.CRYPTO or c.kind != ContractKind.LINEAR_DERIVATIVE:
            raise ValueError("Nautilus linear bridge refuses non-crypto and non-linear instruments")
        if self.settlement_currency != c.quote_currency:
            raise ValueError("linear contract must settle in its quoted collateral currency")
        if (
            not self.base_currency.isascii()
            or not self.base_currency.isalpha()
            or not 2 <= len(self.base_currency) <= 8
        ):
            raise ValueError("invalid base currency")
        if self.base_currency.upper() != self.base_currency or self.base_currency == c.quote_currency:
            raise ValueError("unqualified base currency")
        # Explicitly prevent accidental spot/perpetual substitutions and
        # assumptions about base, quote, multiplier or regional contract.
        if c.symbol != f"{self.base_currency}-{c.quote_currency}-SWAP":
            raise ValueError("contract symbol/base/quote/settlement binding disagrees")
        _precision(c.tick_size)
        _precision(c.quantity_step)
        if type(self.definition_ns) is not int or type(self.observed_ns) is not int:
            raise TypeError("contract timestamps must be integer UTC nanoseconds")
        if not 0 <= self.definition_ns <= self.observed_ns <= MAX_EVENT_TIMESTAMP_NS:
            raise ValueError("future/invalid contract definition time")
        if self.metadata_source not in {"offline_fixture", "public_read_only"}:
            raise ValueError("external authenticated metadata source not yet qualified")
        for name in ("maker_fee_bps", "taker_fee_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be an exact Decimal")
            decimal(value)
            if not Decimal("-100") <= value <= Decimal("1000"):
                raise ValueError(f"{name} outside conservative simulation bounds")


def to_nautilus_perpetual(spec: LinearContractDefinition) -> CryptoPerpetual:
    """Register a priced crypto swap in Nautilus *for offline replay only*.

    Adapter execution capabilities remain disabled elsewhere even if this
    model object exists. The rate/contract fields are scenario assumptions,
    not authenticated from OKX EEA, and may not be used for broker routing.
    """
    c = spec.instrument
    qp = _precision(c.quantity_step)
    pp = _precision(c.tick_size)
    quote = Currency.from_str(c.quote_currency)
    base = Currency.from_str(spec.base_currency)
    return CryptoPerpetual(
        instrument_id=nautilus_instrument_id(c),
        raw_symbol=Symbol(c.symbol),
        base_currency=base,
        quote_currency=quote,
        settlement_currency=quote,
        is_inverse=False,
        price_precision=pp,
        size_precision=qp,
        price_increment=Price.from_str(f"{c.tick_size:.{pp}f}"),
        size_increment=Quantity.from_str(f"{c.quantity_step:.{qp}f}"),
        multiplier=Quantity.from_str(str(c.multiplier)),
        min_quantity=Quantity.from_str(f"{c.min_quantity:.{qp}f}"),
        min_notional=Money.from_str(f"{c.min_notional} {c.quote_currency}"),
        margin_init=c.initial_margin,
        margin_maint=c.maintenance_margin,
        maker_fee=spec.maker_fee_bps / Decimal("10000"),
        taker_fee=spec.taker_fee_bps / Decimal("10000"),
        ts_event=spec.definition_ns,
        ts_init=spec.observed_ns,
    )
