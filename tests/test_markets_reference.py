"""D1 invariant tests for offline cross-venue foundations."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lab.markets import (
    AssetClass,
    ContractKind,
    Instrument,
    MarkPrice,
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


def observed(sim: OfflineSimulator, trade: Trade, *, mark: str | None = None):
    """Explicitly provide the independent synthetic valuation feed in fixture tests."""
    sim.on_mark(
        MarkPrice(
            f"valuation-{trade.trade_id}",
            trade.exchange_ns,
            trade.received_ns,
            D(mark) if mark is not None else trade.price,
        )
    )
    return sim.on_trade(trade)


def test_execution_modes_are_unreachable() -> None:
    for mode in (TradingMode.BROKER_DEMO, TradingMode.LIVE):
        with pytest.raises(UnsupportedMode):
            OfflineSimulator(instrument(), mode=mode)


def test_domain_rejects_bad_precision_or_margin() -> None:
    with pytest.raises(ValueError, match="step"):
        Instrument(
            "SIM",
            "BTC-USD-LINEAR",
            AssetClass.CRYPTO,
            ContractKind.LINEAR_DERIVATIVE,
            "USD",
            D("0.1"),
            D("0.003"),
            D("0.005"),
            D("5"),
        )
    with pytest.raises(TypeError):
        OfflineSimulator(instrument(), starting_balance=1000.0)
    with pytest.raises(ValueError, match="quantity"):
        instrument().check_quantity(D("0.0005"))


def test_short_no_proceeds_and_cover_realizes_pnl() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    observed(sim, event(1))
    sim.submit(order("open", Side.SELL, "1", 1_000_000_000))
    assert not observed(sim, event(10, timestamp=1_000_000_000))
    assert len(observed(sim, event(2))) == 1
    assert sim.position == D("-1")
    assert sim.balance == D("1000")  # short sale proceeds do not become free cash
    assert sim.unrealized == D("0")
    sim.submit(order("cover", Side.BUY, "1", 2_000_000_000, reduce_only=True))
    observed(sim, event(3, "90"))
    assert sim.position == 0
    assert sim.balance == D("1010")
    assert sim.total_realized == D("10")


def test_funding_is_explicit_and_signed() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    observed(sim, event(1))
    sim.submit(order("short", Side.SELL, "1", 1_000_000_000))
    observed(sim, event(2))
    paid = sim.apply_funding("fund1", D("0.001"), 3_000_000_000)
    assert paid == D("-0.1")  # short receives funding for positive rate
    assert sim.balance == D("1000.1")
    with pytest.raises(ValueError, match="duplicate"):
        sim.apply_funding("fund1", D("0.001"), 4_000_000_000)


def test_hard_halt_blocks_new_orders_but_allows_cover() -> None:
    limits = SimulationLimits(max_daily_loss=D("0.01"), max_drawdown=D("0.02"))
    sim = OfflineSimulator(instrument(), limits=limits, taker_fee_bps=D("0"), slippage_bps=D("0"))
    observed(sim, event(1))
    sim.submit(order("short", Side.SELL, "1", 1_000_000_000))
    observed(sim, event(2))
    observed(sim, event(3, "130"))
    assert sim.halt_reason
    with pytest.raises(ValueError, match="hard halt"):
        sim.submit(order("new", Side.SELL, "1", 3_000_000_000))
    sim.submit(order("exit", Side.BUY, "1", 3_000_000_000, reduce_only=True))
    observed(sim, event(4, "130"))
    assert sim.position == 0


def test_reduction_never_reverses_and_order_ids_are_unique() -> None:
    sim = OfflineSimulator(instrument())
    observed(sim, event(1))
    with pytest.raises(ValueError, match="reduce-only"):
        sim.submit(order("cover", Side.BUY, "1", 1_000_000_000, reduce_only=True))
    sim.submit(order("unique", Side.BUY, "1", 1_000_000_000))
    assert sim.cancel("unique")
    with pytest.raises(ValueError, match="duplicate"):
        sim.submit(order("unique", Side.BUY, "1", 1_000_000_000))


def test_operator_risk_prevents_oversized_exposure() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    observed(sim, event(1))
    sim.submit(order("big", Side.BUY, "9", 1_000_000_000))
    assert not observed(sim, event(2))
    assert not sim.position
    assert "risk ceilings" in sim.rejections[0][1]


def test_first_fill_is_strictly_after_decision() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    observed(sim, event(1))
    sim.submit(order("entry", Side.BUY, "1", 1_000_000_000))
    observed(sim, event(4, timestamp=1_000_000_000))
    assert sim.position == 0
    observed(sim, event(5))
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
        observed(sim, event(1, "100.05"))


def test_no_spot_short_in_linear_reference() -> None:
    spot = Instrument(
        "SIM",
        "BTC-USD",
        AssetClass.CRYPTO,
        ContractKind.SPOT,
        "USD",
        D("0.1"),
        D("0.001"),
        D("0.001"),
        D("5"),
    )
    with pytest.raises(ValueError, match="linear derivatives"):
        OfflineSimulator(spot)


def test_adapter_protocol_cannot_route_demo_or_real_money() -> None:
    from lab.markets.adapters import ExecutionAdapter, OfflineAdapter, ReplayDataAdapter, open_execution_adapter

    adapter = open_execution_adapter(TradingMode.OFFLINE_SIM, instrument())
    assert isinstance(adapter, ExecutionAdapter)
    assert isinstance(adapter, ReplayDataAdapter)
    assert isinstance(adapter, OfflineAdapter)
    for mode in (TradingMode.BROKER_DEMO, TradingMode.LIVE):
        with pytest.raises(UnsupportedMode, match="only offline"):
            open_execution_adapter(mode, instrument())


def test_offline_cli_fixture_smoke() -> None:
    from importlib.resources import files
    from pathlib import Path

    from lab.markets.__main__ import demo

    path = Path(str(files("lab.markets").joinpath("fixture.csv")))
    result = demo(path, D("0.001"), Side.SELL, D("1000"))
    assert result["mode"] == "offline_sim"
    assert result["fills"] == 2
    assert result["position"] == "0.000"
    assert result["pending_orders"] == 0
    assert result["minute_bars"] == 3
    assert result["market_data_validated"] is False
    assert result["qualified_for_live_trading"] is False


def test_funding_events_prevent_time_travel_in_subsequent_trades() -> None:
    sim = OfflineSimulator(instrument())
    observed(sim, event(1))
    sim.apply_funding("fund1", D("0.001"), 5_000_000_000)
    with pytest.raises(ValueError, match="out-of-order"):
        observed(sim, event(2))
    observed(sim, event(6))


def test_minute_bar_dedup_does_not_accumulate_all_historical_ids() -> None:
    builder = MinuteBuilder()
    builder.push(event(1, timestamp=1_000_000_000))
    assert len(builder._trade_ids) == 1
    builder.flush(60_000_000_000)
    assert not builder._trade_ids


def test_last_trade_wick_does_not_liquidate_with_healthy_mark() -> None:
    sim = OfflineSimulator(
        instrument(),
        limits=SimulationLimits(max_drawdown=D("0.02"), max_daily_loss=D("0.02")),
        taker_fee_bps=D("0"),
        slippage_bps=D("0"),
    )
    observed(sim, event(1, "100"))
    sim.submit(order("short1", Side.SELL, "1", 1_000_000_000))
    observed(sim, event(2, "100"))
    assert sim.position == D("-1")
    assert sim.mark == D("100")
    # Independent trade and mark series: thin-book flash last price of 999.
    sim.on_trade(event(3, "999"))
    assert sim.mark == D("100")
    assert sim.last_trade_price == D("999")
    assert sim.unrealized == 0
    assert sim.halt_reason is None
    # A genuine mark-price move, unlike a trade wick, MUST update equity/risk.
    sim.on_mark(MarkPrice("newmark", 4_000_000_000, 4_000_000_000, D("130")))
    assert sim.halt_reason is not None


@pytest.mark.parametrize(
    ("side", "rate", "expected"),
    [
        (Side.BUY, "0.01", "-1"),
        (Side.BUY, "-0.01", "1"),
        (Side.SELL, "0.01", "1"),
        (Side.SELL, "-0.01", "-1"),
    ],
)
def test_funding_both_directions_on_both_sides(side: Side, rate: str, expected: str) -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    observed(sim, event(1, "100"))
    sim.submit(order("entry", side, "1", 1_000_000_000))
    observed(sim, event(2, "100"))
    assert sim.position == D(side.sign)
    debit = sim.apply_funding("settlement", D(rate), 3_000_000_000)
    assert debit == -D(expected)
    assert sim.balance == D("1000") + D(expected)


def test_stale_mark_refuses_new_risk_even_with_recent_trade() -> None:
    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    observed(sim, event(1, "100"))
    sim.on_trade(event(2, "100", timestamp=150_000_000_000))
    with pytest.raises(ValueError, match="stale"):
        sim.submit(order("stale", Side.BUY, "1", 150_000_000_000))


def test_simulator_shares_one_trade_participation_budget_across_pending_orders() -> None:
    sim = OfflineSimulator(
        instrument(),
        starting_balance=D("10000"),
        taker_fee_bps=D("0"),
        slippage_bps=D("0"),
    )
    sim.on_mark(MarkPrice("mark1", 1, 1, D("100")))
    sim.on_trade(Trade("trade1", 1, 1, D("100"), D("10")))
    sim.submit(order("longA", Side.BUY, "0.75", 1))
    sim.submit(order("longB", Side.BUY, "0.75", 1))
    sim.on_mark(MarkPrice("mark2", 2, 2, D("100")))
    fills = sim.on_trade(Trade("trade2", 2, 2, D("100"), D("10")))
    assert [fill.quantity for fill in fills] == [D("0.75"), D("0.25")]
    assert sum(fill.quantity for fill in fills) == D("1.00")
    assert sim.position == D("1")
    assert sim.pending["longB"].remaining == D("0.50")
    sim.on_mark(MarkPrice("mark3", 3, 3, D("100")))
    second = sim.on_trade(Trade("trade3", 3, 3, D("100"), D("10")))
    assert [fill.quantity for fill in second] == [D("0.50")]
    assert sim.position == D("1.5")
    assert not sim.pending


def test_invalid_trade_does_not_poison_duplicate_state_or_ledger() -> None:
    sim = OfflineSimulator(instrument())
    observed(sim, event(1))
    bad = Trade("reused", 2_000_000_000, 2_000_000_000, D("100.05"), D("10"))
    with pytest.raises(ValueError, match="tick"):
        sim.on_trade(bad)
    assert sim.last_ns == 1_000_000_000
    assert sim.last_trade_id == "t1"
    assert "reused" not in sim._trade_ids_at_timestamp
    good = Trade("reused", 2_000_000_000, 2_000_000_000, D("100"), D("10"))
    assert sim.on_trade(good) == ()
    assert sim.last_trade_id == "reused"


def test_delayed_receipt_does_not_seal_valid_next_minute_events() -> None:
    builder = MinuteBuilder()
    first = Trade("first", 10_000_000_000, 600_000_000_000, D("100"), D("1"))
    second = Trade("second", 60_000_000_001, 601_000_000_000, D("110"), D("1"))
    third = Trade("third", 60_000_000_002, 602_000_000_000, D("115"), D("1"))
    assert builder.push(first) == ()
    sealed = builder.push(second)
    assert len(sealed) == 1
    assert sealed[0].start_ns == 0
    assert builder.push(third) == ()
    current = builder.flush(120_000_000_000)
    assert len(current) == 1
    assert current[0].open == D("110")
    assert current[0].close == D("115")
    assert current[0].trades == 2


def test_mark_prices_are_not_required_to_match_order_tick_precision() -> None:
    sim = OfflineSimulator(instrument())
    # Mark/index price can carry finer precision than the exchange trading tick.
    sim.on_mark(MarkPrice("fine-mark", 1, 1, D("100.012345")))
    assert sim.mark == D("100.012345")
    assert sim.equity == sim.balance
    # Execution prices still obey the instrument tick specification.
    with pytest.raises(ValueError, match="tick"):
        sim.on_trade(Trade("bad-trade", 2, 2, D("100.05"), D("1")))


def test_replay_ids_are_unique_across_nonadjacent_events() -> None:
    sim = OfflineSimulator(instrument())
    sim.on_mark(MarkPrice("mark-A", 1, 1, D("100")))
    sim.on_trade(Trade("trade-A", 1, 1, D("100"), D("10")))
    sim.on_mark(MarkPrice("mark-B", 2, 2, D("100")))
    sim.on_trade(Trade("trade-B", 2, 2, D("100"), D("10")))
    with pytest.raises(ValueError, match="duplicate mark"):
        sim.on_mark(MarkPrice("mark-A", 3, 3, D("100")))
    with pytest.raises(ValueError, match="duplicate trade"):
        sim.on_trade(Trade("trade-A", 3, 3, D("100"), D("10")))
    assert sim.last_event_ns == 2
