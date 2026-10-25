"""Executable D1 acceptance matrix for exact linear-derivative position transitions.

The pure accounting oracle is called by the offline simulator; a future
Nautilus bridge must call this *same* implementation, not duplicate it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lab.markets.accounting import linear_transition, linear_unrealized

D = Decimal


@pytest.mark.parametrize(
    ("current", "entry", "delta", "fill", "multiplier", "position", "average", "realized"),
    [
        # Same-direction scale-in: weighted entry, no realized profit.
        ("2", "100", "1", "130", "5", "3", "110", "0"),
        ("-2", "100", "-1", "130", "5", "-3", "110", "0"),
        # Partial closing: preserve prior entry on surviving contracts.
        ("2", "100", "-1", "130", "5", "1", "100", "150"),
        ("-2", "100", "1", "70", "5", "-1", "100", "150"),
        ("2", "100", "-1", "70", "5", "1", "100", "-150"),
        ("-2", "100", "1", "130", "5", "-1", "100", "-150"),
        # Full flat: no entry price remains and all closed contracts realize PnL.
        ("2", "100", "-2", "130", "5", "0", "0", "300"),
        ("-2", "100", "2", "70", "5", "0", "0", "300"),
        # Atomic sign flip: close old book, open residual at new fill price.
        ("2", "100", "-3", "130", "5", "-1", "130", "300"),
        ("-2", "100", "3", "70", "5", "1", "70", "300"),
        ("2", "100", "-3", "70", "5", "-1", "70", "-300"),
        ("-2", "100", "3", "130", "5", "1", "130", "-300"),
        # Do not invent realized pnl for unchanged positions or new entry.
        ("0", "0", "2", "100", "5", "2", "100", "0"),
        ("2", "100", "0", "120", "5", "2", "100", "0"),
    ],
)
def test_transition_branches(
    current: str,
    entry: str,
    delta: str,
    fill: str,
    multiplier: str,
    position: str,
    average: str,
    realized: str,
) -> None:
    out = linear_transition(D(current), D(entry), D(delta), D(fill), D(multiplier))
    assert out.position == D(position)
    assert out.entry_price == D(average)
    assert out.realized_pnl == D(realized)


@pytest.mark.parametrize(
    ("current", "delta", "entry", "fill", "multiplier"),
    [
        ("2", "1", "100", "130", "5"),
        ("-2", "-1", "100", "130", "5"),
        ("2", "-1", "100", "130", "5"),
        ("-2", "1", "100", "70", "5"),
        ("2", "-3", "100", "130", "5"),
        ("-2", "3", "100", "70", "5"),
    ],
)
def test_transition_conserves_marked_equity(
    current: str,
    delta: str,
    entry: str,
    fill: str,
    multiplier: str,
) -> None:
    """At an instantaneous fill and zero fee, realized + residual UPL = prior UPL."""
    old_pos = D(current)
    px = D(fill)
    unit = D(multiplier)
    previous = linear_unrealized(old_pos, D(entry), px, unit)
    out = linear_transition(old_pos, D(entry), D(delta), px, unit)
    resulting = out.realized_pnl + linear_unrealized(out.position, out.entry_price, px, unit)
    assert resulting == previous


def test_sign_flip_commits_one_fill_and_charges_fee_on_full_notional() -> None:
    from lab.markets import (
        AssetClass,
        ContractKind,
        Instrument,
        MarkPrice,
        OfflineSimulator,
        OrderIntent,
        Side,
        Trade,
    )

    instrument = Instrument(
        "SIM",
        "BTC-USD-LINEAR",
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USD",
        D("0.1"),
        D("0.001"),
        D("0.001"),
        D("5"),
        multiplier=D("1"),
    )
    sim = OfflineSimulator(
        instrument,
        starting_balance=D("10000"),
        taker_fee_bps=D("5"),
        slippage_bps=D("0"),
    )
    for idx, px in ((1, "100"), (2, "100")):
        sim.on_mark(MarkPrice(f"m{idx}", idx, idx, D(px)))
        sim.on_trade(Trade(f"t{idx}", idx, idx, D(px), D("100")))
        if idx == 1:
            sim.submit(OrderIntent("openlong", instrument.instrument_id, Side.BUY, D("2"), idx))
    assert sim.position == D("2")
    assert sim.entry == D("100")
    sim.submit(OrderIntent("reverse", instrument.instrument_id, Side.SELL, D("3"), 2))
    sim.on_mark(MarkPrice("m3", 3, 3, D("130")))
    fills = sim.on_trade(Trade("t3", 3, 3, D("130"), D("100")))
    assert len(fills) == 1
    assert fills[0].quantity == D("3")
    assert fills[0].realized_pnl == D("60")
    assert fills[0].fee == D("3") * D("130") * D("5") / D("10000")
    assert sim.position == D("-1")
    assert sim.entry == D("130")
    assert sim.balance == D("10000") + D("60") - sim.total_fees
