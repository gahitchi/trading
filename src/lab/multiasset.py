"""Currency-accurate instrument and FX market-data contracts (research-only).

No broker adapter, order sending, margin financing or execution permission. Cash
movements from spot FX are modeled separately from securities positions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

_CCY = re.compile(r"[A-Z]{3}")


class MultiAssetError(ValueError):
    """Unknown instrument, stale quote or unsupported funding/risk assumptions."""


def _currency(value: str) -> str:
    if not isinstance(value, str) or not _CCY.fullmatch(value):
        raise MultiAssetError(f"invalid ISO-style currency code: {value!r}")
    return value


def _decimal(value: Decimal, label: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MultiAssetError(f"{label} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise MultiAssetError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return value


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise MultiAssetError("timestamp must be UTC-aware")
    return value.astimezone(UTC)


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    BOND = "BOND"
    FX_SPOT = "FX_SPOT"


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    asset_class: AssetClass
    quote_currency: str
    quantity_step: Decimal
    price_tick: Decimal
    min_quantity: Decimal
    base_currency: str | None = None

    def __post_init__(self):
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise MultiAssetError("permanent instrument ID required")
        if not isinstance(self.asset_class, AssetClass):
            raise MultiAssetError("unsupported asset class")
        _currency(self.quote_currency)
        for k in ("quantity_step", "price_tick", "min_quantity"):
            _decimal(getattr(self, k), k, positive=True)
        if self.min_quantity % self.quantity_step != 0:
            raise MultiAssetError("min_quantity must be aligned to quantity_step")
        if self.asset_class == AssetClass.FX_SPOT:
            if self.base_currency is None or _currency(self.base_currency) == self.quote_currency:
                raise MultiAssetError("FX base and quote currency must be distinct")
        elif self.base_currency is not None:
            raise MultiAssetError("base_currency is allowed only for spot FX")
        if self.asset_class == AssetClass.BOND and self.price_tick >= Decimal("100"):
            raise MultiAssetError("bond price tick is quoted per 100 face and must be <100")

    def validate_quantity(self, qty: Decimal) -> None:
        _decimal(qty, "quantity", positive=True)
        if qty < self.min_quantity or qty % self.quantity_step:
            raise MultiAssetError("quantity violates minimum or step for this instrument")

    def validate_price(self, px: Decimal) -> None:
        _decimal(px, "price", positive=True)
        if px % self.price_tick:
            raise MultiAssetError("price violates instrument tick size")


@dataclass(frozen=True, slots=True)
class FXQuote:
    base: str
    quote: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    source: str

    def __post_init__(self):
        _currency(self.base)
        _currency(self.quote)
        if self.base == self.quote:
            raise MultiAssetError("FX quote currencies must be different")
        _decimal(self.bid, "bid", positive=True)
        _decimal(self.ask, "ask", positive=True)
        if self.ask < self.bid:
            raise MultiAssetError("crossed FX bid/ask quote")
        _time(self.timestamp)
        if not isinstance(self.source, str) or not self.source.strip():
            raise MultiAssetError("FX quote provenance required")


class FXRates:
    """Conservative side-aware conversion with bounded paths and stale checks."""

    def __init__(self, quotes: list[FXQuote]):
        self._quotes: dict[tuple[str, str], FXQuote] = {}
        parent: dict[str, str] = {}

        def find(currency: str) -> str:
            parent.setdefault(currency, currency)
            while parent[currency] != currency:
                currency = parent[currency]
            return currency

        for quote in quotes:
            key = (quote.base, quote.quote)
            if key in self._quotes or (quote.quote, quote.base) in self._quotes:
                raise MultiAssetError(f"duplicate or inverted FX quote pair: {key}")
            a, b = find(quote.base), find(quote.quote)
            if a == b:
                raise MultiAssetError("cyclic FX graph gives conflicting conversion paths; use a verified quote tree")
            parent[a] = b
            self._quotes[key] = quote

    def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        *,
        as_of: datetime,
        max_age: timedelta,
        max_hops: int = 2,
    ) -> Decimal:
        """Liquidation-side estimate: positive amounts sold at bid, liabilities at ask.

        Negative amounts return a negative conservative replacement cost. Never
        cross via a future/stale quote or an unbounded conversion chain.
        """
        if not isinstance(amount, Decimal) or not amount.is_finite():
            raise MultiAssetError("amount must be finite Decimal")
        _currency(from_currency)
        _currency(to_currency)
        now = _time(as_of)
        if not isinstance(max_age, timedelta) or not timedelta(0) < max_age <= timedelta(days=7):
            raise MultiAssetError("max_age must be positive and <=7 days")
        if type(max_hops) is not int or not 1 <= max_hops <= 3:
            raise MultiAssetError("max_hops must be 1-3")
        if amount == 0 or from_currency == to_currency:
            return amount
        graph: dict[str, list[tuple[str, Decimal]]] = {}
        for (base, quote), price in self._quotes.items():
            age = now - _time(price.timestamp)
            if age < timedelta(0) or age > max_age:
                continue
            if amount > 0:
                a, b = price.bid, Decimal(1) / price.ask
            else:
                a, b = price.ask, Decimal(1) / price.bid
            graph.setdefault(base, []).append((quote, a))
            graph.setdefault(quote, []).append((base, b))
        # Exact shortest path, lexical tie-break; never pretend cross conversion
        # was actually executable at the same time or without venue constraints.
        frontier = [(from_currency, Decimal(1), (from_currency,))]
        for _ in range(max_hops):
            nxt = []
            for current, factor, used in frontier:
                for neighbor, rate in sorted(graph.get(current, [])):
                    if neighbor in used:
                        continue
                    price = factor * rate
                    if neighbor == to_currency:
                        return amount * price
                    nxt.append((neighbor, price, (*used, neighbor)))
            frontier = nxt
        raise MultiAssetError(f"no fresh {from_currency}->{to_currency} quote path within {max_hops} hops")


@dataclass(frozen=True, slots=True)
class MarketPrice:
    instrument_id: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    accrued_interest_per_100: Decimal = Decimal("0")

    def __post_init__(self):
        if not self.instrument_id:
            raise MultiAssetError("instrument ID required")
        _decimal(self.bid, "bid", positive=True)
        _decimal(self.ask, "ask", positive=True)
        if self.ask < self.bid:
            raise MultiAssetError("crossed market price")
        _decimal(self.accrued_interest_per_100, "accrued interest")
        _time(self.timestamp)


@dataclass(frozen=True, slots=True)
class Holding:
    instrument_id: str
    quantity: Decimal

    def __post_init__(self):
        if not self.instrument_id or not isinstance(self.quantity, Decimal) or not self.quantity.is_finite():
            raise MultiAssetError("holding requires instrument and finite Decimal units")


def cash_after_spot_trade(
    cash: dict[str, Decimal],
    instrument: Instrument,
    *,
    side: str,
    base_quantity: Decimal,
    market: MarketPrice,
    commission_quote: Decimal,
    as_of: datetime,
    max_age: timedelta,
) -> dict[str, Decimal]:
    """Return *proposed* currency cash movements; no broker orders or settlement.

    Default cash-only policy rejects any negative bucket. Spot FX creates two
    currency cash legs, never a duplicated FX security holding.
    """
    if instrument.asset_class != AssetClass.FX_SPOT or instrument.instrument_id != market.instrument_id:
        raise MultiAssetError("FX spot instrument/price mismatch")
    if side not in ("BUY", "SELL"):
        raise MultiAssetError("side must be BUY or SELL")
    instrument.validate_quantity(base_quantity)
    _decimal(commission_quote, "commission")
    age = _time(as_of) - _time(market.timestamp)
    if not isinstance(max_age, timedelta) or not timedelta(0) < max_age <= timedelta(days=7):
        raise MultiAssetError("invalid maximum quote age")
    if age < timedelta(0) or age > max_age:
        raise MultiAssetError("future or stale FX execution quote")
    price = market.ask if side == "BUY" else market.bid
    instrument.validate_price(price)
    balances = dict(cash)
    for key, amount in balances.items():
        _currency(key)
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise MultiAssetError("cash ledger must have finite nonnegative balances")
    base = instrument.base_currency
    quote = instrument.quote_currency
    balances.setdefault(base, Decimal(0))
    balances.setdefault(quote, Decimal(0))
    sign = Decimal(1) if side == "BUY" else Decimal(-1)
    balances[base] += sign * base_quantity
    balances[quote] -= sign * base_quantity * price + commission_quote
    if any(value < 0 for value in balances.values()):
        raise MultiAssetError("insufficient settled cash; no leverage or assumed borrowing")
    return balances


def portfolio_liquidation_value(
    *,
    cash: dict[str, Decimal],
    holdings: list[Holding],
    instruments: dict[str, Instrument],
    prices: dict[str, MarketPrice],
    fx: FXRates,
    reporting_currency: str,
    as_of: datetime,
    max_age: timedelta,
) -> Decimal:
    """Conservative research mark across currencies, shares and bonds.

    For bond face-amount units, quoted clean price and accrued interest are
    expressed per 100 par. Short securities and financing are unsupported.
    """
    _currency(reporting_currency)
    now = _time(as_of)
    amount = Decimal(0)
    for ccy, balance in cash.items():
        _currency(ccy)
        _decimal(balance, f"cash {ccy}")
        amount += fx.convert(balance, ccy, reporting_currency, as_of=now, max_age=max_age)
    seen = set()
    for holding in holdings:
        if holding.instrument_id in seen:
            raise MultiAssetError("duplicate holding; consolidate explicitly")
        seen.add(holding.instrument_id)
        inst = instruments.get(holding.instrument_id)
        price = prices.get(holding.instrument_id)
        if inst is None or price is None or price.instrument_id != holding.instrument_id:
            raise MultiAssetError("missing instrument or market price")
        if inst.asset_class == AssetClass.FX_SPOT:
            raise MultiAssetError("spot FX must be represented by cash legs, not a security holding")
        if holding.quantity < 0:
            raise MultiAssetError("short securities and borrowing are not supported")
        if holding.quantity == 0:
            continue
        inst.validate_quantity(holding.quantity)
        inst.validate_price(price.bid)
        inst.validate_price(price.ask)
        age = now - _time(price.timestamp)
        if age < timedelta(0) or age > max_age:
            raise MultiAssetError("future or stale security mark")
        divisor = Decimal(100) if inst.asset_class == AssetClass.BOND else Decimal(1)
        if inst.asset_class != AssetClass.BOND and price.accrued_interest_per_100:
            raise MultiAssetError("accrued interest belongs only to bond instruments")
        gross = holding.quantity * (price.bid + (price.accrued_interest_per_100 if divisor == 100 else 0)) / divisor
        amount += fx.convert(gross, inst.quote_currency, reporting_currency, as_of=now, max_age=max_age)
    return amount
