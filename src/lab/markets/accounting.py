"""Pure, exact Decimal accounting for a *linear* quote-collateral derivative.

Ground-truth formulas shared by the offline reference and the future Nautilus
crypto bridge. Not a second allocator, strategy, or risk-decision subsystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lab.markets.domain import decimal, positive


@dataclass(frozen=True, slots=True)
class PositionTransition:
    position: Decimal
    entry_price: Decimal
    realized_pnl: Decimal


def linear_unrealized(position: Decimal, entry: Decimal, mark: Decimal, multiplier: Decimal) -> Decimal:
    decimal(position)
    if position == 0:
        return Decimal("0")
    positive(decimal(entry), "entry")
    positive(decimal(mark), "mark")
    positive(decimal(multiplier), "contract multiplier")
    return (mark - entry) * position * multiplier


def linear_notional(position: Decimal, mark: Decimal, multiplier: Decimal) -> Decimal:
    decimal(position)
    positive(decimal(mark), "mark")
    positive(decimal(multiplier), "contract multiplier")
    return abs(position * mark * multiplier)


def linear_transition(
    current: Decimal, entry: Decimal, delta: Decimal, fill: Decimal, multiplier: Decimal
) -> PositionTransition:
    decimal(current)
    decimal(delta)
    positive(decimal(fill), "fill")
    positive(decimal(multiplier), "contract multiplier")
    if current:
        positive(decimal(entry), "entry")
    new = current + delta
    if current == 0 or current * delta >= 0:
        avg = ((abs(current) * entry + abs(delta) * fill) / abs(new)) if new else Decimal("0")
        return PositionTransition(new, avg, Decimal("0"))
    closed = min(abs(current), abs(delta))
    realized = (fill - entry) * closed * multiplier * (1 if current > 0 else -1)
    average = Decimal("0") if new == 0 else (entry if current * new > 0 else fill)
    return PositionTransition(new, average, realized)


def signed_funding_payment(position: Decimal, mark: Decimal, multiplier: Decimal, rate: Decimal) -> Decimal:
    """Positive result is a debit; long pays on +rate, short pays on -rate."""
    decimal(position)
    decimal(rate)
    positive(decimal(mark), "mark")
    positive(decimal(multiplier), "contract multiplier")
    return position * mark * multiplier * rate
