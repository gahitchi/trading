"""
Margin and financing arithmetic for signed (long/short) books. Pure functions, no state.

Conventions: values in account currency; `long_value` and `short_value` are both >= 0
(market value of long positions, and of shorted shares). `cash` is the ledger's cash,
which includes the proceeds of short sales.

Requirements follow Reg T / FINRA by default (lab.toml [margin]); IBKR applies these or
stricter house rules, so the agents keep a cushion above them and deleverage first.
"""

from __future__ import annotations

import math

from lab.config import MarginSettings

DAY = 1 / 365


def initial_requirement(weights: dict[str, float], m: MarginSettings) -> float:
    """Initial margin needed for target weights, as a fraction of equity (must stay <= 1)."""
    return sum((m.initial_long if w > 0 else m.initial_short) * abs(w) for w in weights.values())


def maintenance_requirement(long_value: float, short_value: float, m: MarginSettings) -> float:
    return m.maintenance_long * long_value + m.maintenance_short * short_value


def deleverage_scale(equity: float, long_value: float, short_value: float, m: MarginSettings) -> float:
    """Factor (<= 1) to scale every position by so equity covers maintenance with the cushion. 1 = fine."""
    need = maintenance_requirement(long_value, short_value, m) * (1 + m.cushion)
    if need <= 0 or equity >= need:
        return 1.0
    return max(0.0, equity / need)


def borrowed_cash(cash: float, short_value: float) -> float:
    """Cash borrowed from the broker. Short proceeds are not counted as available (conservative): a book that is
    long with its own money plus short-sale proceeds is financing those longs with borrowed money."""
    return max(0.0, short_value - cash)


def financing_cost(
    cash: float, short_value: float, days: float, m: MarginSettings, *, verified_borrow_fee: float | None = None
) -> tuple[float, float]:
    """(borrow fee, debit interest) for holding the book `days` calendar days.

    When a point-in-time BorrowBook validated the whole short book, its exact
    cost can replace the legacy uniform assumed APR. Otherwise retain the
    explicitly disclosed *assumed* research fee, never claim it was verified.
    """
    if verified_borrow_fee is not None and (
        not isinstance(verified_borrow_fee, (int, float))
        or not math.isfinite(verified_borrow_fee)
        or verified_borrow_fee < 0
    ):
        raise ValueError("verified_borrow_fee must be finite and nonnegative")
    borrow = verified_borrow_fee if verified_borrow_fee is not None else short_value * m.borrow_apr * days * DAY
    debit = borrowed_cash(cash, short_value) * m.debit_apr * days * DAY
    return borrow, debit
