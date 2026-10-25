"""D1 invariant tests for offline cross-venue foundations."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lab.markets import (
    AssetClass,
    ContractKind,
    Instrument,
    MinuteBuilder,
    OfflineSimulator,
    OrderIntent,
    Side,
    SimulationLimits,
    Trade,
    TradingMode,
)
from lab.markets.domain import UnsupportedMode

D = Decimal


def instrument() -> Instrument:
    return Instrument(
        venue="SIM",
        symbol="BTC-USD-LINEAR",
        asset_class=AssetClass.CRYPTO,
        kind=ContractKind.LINEAR_DERIVATIVE,
        quote_currency="USD",
        tick_size=D("0.1"),
        quantity_step=D("0.001"),
        min_quantity=D("0.001"),
        min_notional=D("5"),
        multiplier=D("1"),
    )


def event(idx: int, px: str = "100", *, timestamp: int | None = None) -> Trade:
    ns = idx * 1_000_000_000 if timestamp is None else timestamp
    return Trade(f"t{idx}", ns, ns, D(px), D("100"))


def order(id_: str, side: Side, qty: str, ts: int, *, reduce_only: bool = False) -> OrderIntent:
    return OrderIntent(id_, instrument().instrument_id, side, D(qty), ts, reduce_only=reduce_only)


def test_execution_modes_are_unreachable() -> None:
    for mode in (TradingMode.BROKER_DEMO, TradingMode.LIVE):
        with pytest.raises(UnsupportedMode):
            OfflineSimulator(instrument(), mode=mode)


def test_domain_rejects_bad_precision_or_margin() -> None:
    with pytest.raises(ValueError, match="step"):
        Instrument(
            "SIM", "BTC-USD-LINEAR", AssetClass.CRYPTO, ContractKind.LINEAR_DERIVATIVE,
            "USD", D("0.1"), D("0.003"), D("0.005"), D("5"),
        )
    with pytest.raises(TypeError):
        OfflineSimulator(instrument(), starting_balance=1000.0)
    with pytest.raises(ValueError, match="quantity"):
        instrument().check_quantity(D("0.0005"))


def test_short_no_proceeds_and_cover_realizes_pnl() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    sim.on_trade(event(1))
    sim.submit(order("open", Side.SELL, "1", 1_000_000_000))
    assert not sim.on_trade(event(10, timestamp=1_000_000_000))
    assert len(sim.on_trade(event(2))) == 1
    assert sim.position == D("-1")
    assert sim.balance == D("1000")  # short sale proceeds do not become free cash
    assert sim.unrealized == D("0")
    sim.submit(order("cover", Side.BUY, "1", 2_000_000_000, reduce_only=True))
    sim.on_trade(event(3, "90"))
    assert sim.position == 0
    assert sim.balance == D("1010")
    assert sim.total_realized == D("10")


def test_funding_is_explicit_and_signed() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    sim.on_trade(event(1))
    sim.submit(order("short", Side.SELL, "1", 1_000_000_000))
    sim.on_trade(event(2))
    paid = sim.apply_funding("fund1", D("0.001"), 3_000_000_000)
    assert paid == D("-0.1")  # short receives funding for positive rate
    assert sim.balance == D("1000.1")
    with pytest.raises(ValueError, match="duplicate"):
        sim.apply_funding("fund1", D("0.001"), 4_000_000_000)


def test_hard_halt_blocks_new_orders_but_allows_cover() -> None:
    limits = SimulationLimits(max_daily_loss=D("0.01"), max_drawdown=D("0.02"))
    sim = OfflineSimulator(instrument(), limits=limits, taker_fee_bps=D("0"), slippage_bps=D("0"))
    sim.on_trade(event(1))
    sim.submit(order("short", Side.SELL, "1", 1_000_000_000))
    sim.on_trade(event(2))
    sim.on_trade(event(3, "130"))
    assert sim.halt_reason
    with pytest.raises(ValueError, match="hard halt"):
        sim.submit(order("new", Side.SELL, "1", 3_000_000_000))
    sim.submit(order("exit", Side.BUY, "1", 3_000_000_000, reduce_only=True))
    sim.on_trade(event(4, "130"))
    assert sim.position == 0


def test_reduction_never_reverses_and_order_ids_are_unique() -> None:
    sim = OfflineSimulator(instrument())
    sim.on_trade(event(1))
    with pytest.raises(ValueError, match="reduce-only"):
        sim.submit(order("cover", Side.BUY, "1", 1_000_000_000, reduce_only=True))
    sim.submit(order("unique", Side.BUY, "1", 1_000_000_000))
    assert sim.cancel("unique")
    with pytest.raises(ValueError, match="duplicate"):
        sim.submit(order("unique", Side.BUY, "1", 1_000_000_000))


def test_operator_risk_prevents_oversized_exposure() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    sim.on_trade(event(1))
    sim.submit(order("big", Side.BUY, "9", 1_000_000_000))
    assert not sim.on_trade(event(2))
    assert not sim.position
    assert "risk ceilings" in sim.rejections[0][1]


def test_first_fill_is_strictly_after_decision() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    sim.on_trade(event(1))
    sim.submit(order("entry", Side.BUY, "1", 1_000_000_000))
    sim.on_trade(event(4, timestamp=1_000_000_000))
    assert sim.position == 0
    sim.on_trade(event(5))
    assert sim.position == 1


def test_bars_align_to_utc_report_missing_minutes() -> None:
    builder = MinuteBuilder()
    assert builder.push(event(1, timestamp=15_000_000_000)) == ()
    done = builder.push(event(2, "110", timestamp=180_000_000_000))
    assert len(done) == 1 and done[0].start_ns == 0
    assert done[0].close == D("100")
    final = builder.flush(240_000_000_000)
    assert final[0].missing_minutes_before == 2
    assert final[0].start_ns == 180_000_000_000


def test_bars_reject_duplicate_and_late_trades() -> None:
    builder = MinuteBuilder()
    trade = event(1)
    builder.push(trade)
    with pytest.raises(ValueError, match="duplicate"):
        builder.push(trade)
    builder.flush(61_000_000_000)
    with pytest.raises(ValueError, match="watermark"):
        builder.push(event(2, timestamp=1_000_000_000))


def test_off_tick_trade_refused() -> None:
    sim = OfflineSimulator(instrument())
    with pytest.raises(ValueError, match="tick"):
        sim.on_trade(event(1, "100.05"))


def test_no_spot_short_in_linear_reference() -> None:
    spot = Instrument(
        "SIM", "BTC-USD", AssetClass.CRYPTO, ContractKind.SPOT, "USD",
        D("0.1"), D("0.001"), D("0.001"), D("5"),
    )
    with pytest.raises(ValueError, match="linear derivatives"):
        OfflineSimulator(spot)
