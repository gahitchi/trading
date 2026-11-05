"""Reference-price taker execution scenarios for exploratory crash research.

A constant BASE quantity is opened at the modelled entry bar and closed
at the modelled exit price. Fees apply to the two executed notionals.
Slippage is an explicit adverse fraction of each reference price.
No order-book replay, funding, partial fills, liquidation or market
impact data are inferred. These scenarios cannot qualify live execution.
"""

from __future__ import annotations

import math

import pandas as pd

MODEL = "reference-taker-fixed-base-two-fill-notional-fees-v1"
# Identical named sensitivity scenarios for every research trial.
# All amounts are basis points per side, not empirically observed costs.
STRESS = (
    ("taker_fee5_slip5", 5.0, 5.0),
    ("taker_fee5_slip10", 5.0, 10.0),
    ("taker_fee5_slip20", 5.0, 20.0),
)


def fill_accounting(
    side: int,
    entry_reference: float,
    exit_reference: float,
    fee_bps: float,
    slippage_bps: float,
) -> dict:
    """Returns fractions of entry-reference notional, for one fixed-base trade.

    side +1 buys then sells; side -1 sells then buys. Slippage is
    adverse on both fills. Commissions are charged on the executed
    entry and exit notionals. Does not include funding or queue risk.
    """
    if type(side) is not int or side not in (-1, 1):
        raise ValueError("execution side must be +1 or -1")
    values = (entry_reference, exit_reference, fee_bps, slippage_bps)
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
        raise ValueError("all execution prices and costs must be finite numbers")
    if entry_reference <= 0 or exit_reference <= 0:
        raise ValueError("execution reference prices must be positive")
    if fee_bps < 0 or slippage_bps < 0 or fee_bps >= 10_000 or slippage_bps >= 10_000:
        raise ValueError("fees/slippage must be nonnegative and below 10000 bps")
    slip = slippage_bps / 10_000
    fee = fee_bps / 10_000
    entry_fill = entry_reference * (1 + side * slip)
    exit_fill = exit_reference * (1 - side * slip)
    if entry_fill <= 0 or exit_fill <= 0:
        raise ValueError("slippage scenario produces an invalid executed price")
    reference_gross = side * (exit_reference / entry_reference - 1)
    executed_gross = side * (exit_fill - entry_fill) / entry_reference
    fees = fee * (entry_fill + exit_fill) / entry_reference
    return {
        "entry_fill": entry_fill,
        "exit_fill": exit_fill,
        "reference_gross": reference_gross,
        "executed_gross": executed_gross,
        "slippage_cost": reference_gross - executed_gross,
        "fee_cost": fees,
        "net": executed_gross - fees,
    }


def rescore(trades: pd.DataFrame, side: int, fee_bps: float, slippage_bps: float) -> pd.DataFrame:
    """Reprice existing trade events without altering signals, exits, or sizing.

    Missing price references must fail instead of silently falling back
    to previously reported net results.
    """
    if not {"entry_reference", "exit_reference", "gross", "net"} <= set(trades):
        raise ValueError("trade ledger lacks entry/exit reference price evidence")
    updated = trades.copy()
    for index in updated.index:
        row = updated.loc[index]
        result = fill_accounting(
            side,
            float(row["entry_reference"]),
            float(row["exit_reference"]),
            fee_bps,
            slippage_bps,
        )
        if not math.isclose(float(row["gross"]), result["reference_gross"], rel_tol=1e-10, abs_tol=1e-12):
            raise ValueError("trade gross return and reference fill prices disagree")
        funding = float(row["funding_return"]) if "funding_return" in updated else 0.0
        if not math.isfinite(funding):
            raise ValueError("funding-adjusted stress scenario has nonfinite settlement P&L")
        updated.loc[index, "net"] = result["net"] + funding
        if "funding_return_upper" in updated:
            upper = float(row["funding_return_upper"])
            if not math.isfinite(upper) or upper < funding:
                raise ValueError("invalid funding P&L bounds in cost stress")
            updated.loc[index, "net_upper"] = result["net"] + upper
    return updated


def stress_summary(trades_by_symbol: dict[str, pd.DataFrame], side: int, start, end, summarize) -> list[dict]:
    """Fixed cost sensitivity on the SAME already-found trades.

    summarize is the study's daily-P&L summary function, injected to
    keep the fill model independent of the strategy implementation.
    """
    from lab.persistence import nullable_numbers

    results = []
    for name, fee, slip in STRESS:
        modified = {symbol: rescore(frame, side, fee, slip) for symbol, frame in trades_by_symbol.items()}
        st = summarize(modified, start, end)
        results.append(
            {
                "name": name,
                "fee_bps_each_side": fee,
                "slippage_bps_each_side": slip,
                "flat_price_round_trip_bps": 2 * (fee + slip),
                "trades": st["trades"],
                "mean_net_bps": nullable_numbers(st["mean_bps"]),
                "apr_pct": nullable_numbers(st["apr"]),
                "sharpe": nullable_numbers(st["sharpe"]),
                "newey_west_daily_t": nullable_numbers(st["t_nw"]),
            }
        )
    return results
