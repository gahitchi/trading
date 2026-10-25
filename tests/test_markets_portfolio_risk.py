"""Conservative preflight across simultaneous orders; never qualified execution."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.markets.domain import AssetClass, ContractKind, Instrument, OrderIntent, Side
from lab.markets.portfolio_risk import OpenPosition, RiskCeilings, preflight_pending

D = Decimal


def instrument(symbol: str = "BTC-USDT-SWAP", *, currency: str = "USDT", margin: str = "0.5") -> Instrument:
    return Instrument(
        "OKX",
        symbol,
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        currency,
        D("0.1"),
        D("0.1"),
        D("0.1"),
        D("5"),
        initial_margin=D(margin),
        maintenance_margin=D("0.2"),
    )


def state(symbol: str = "BTC-USDT-SWAP", *, qty: str = "0", entry: str = "0", mark: str = "100") -> OpenPosition:
    return OpenPosition(instrument(symbol), D(qty), D(entry), D(mark), 100)


def order(name: str, symbol: str, side: Side, qty: str, *, reduce: bool = False) -> OrderIntent:
    return OrderIntent(name, f"OKX:{symbol}", side, D(qty), 100, reduce_only=reduce)


def caps(**changes) -> RiskCeilings:
    baseline = RiskCeilings(
        max_gross=D("0.5"),
        max_instrument_fraction=D("0.5"),
        max_margin_fraction=D("0.65"),
        max_drawdown=D("1"),
        max_daily_loss=D("1"),
        max_loss_currency=D("1000"),
        price_gap_bps=D("0"),
        slippage_bps=D("0"),
        fee_bps=D("0"),
        funding_buffer_bps=D("0"),
    )
    return replace(baseline, **changes)


def evaluate(positions=None, orders=None, *, limits=None, equity=None, peak=None, opening=None):
    return preflight_pending(
        tuple(positions if positions is not None else (state(),)),
        tuple(orders if orders is not None else ()),
        collateral_currency="USDT",
        equity=equity or D("1000"),
        peak_equity=peak or D("1000"),
        day_open_equity=opening or D("1000"),
        asof_ns=100,
        ceilings=limits or caps(),
    )


def test_flat_and_safe_pending_order_are_allowed_only_as_preflight() -> None:
    baseline = evaluate()
    assert baseline.allowed and baseline.tested_scenarios == 1
    good = evaluate(orders=[order("b1", "BTC-USDT-SWAP", Side.BUY, "3")])
    assert good.allowed
    assert good.tested_scenarios == 3
    assert good.worst_gross == D("300")
    assert good.execution_authorized is False
    good.require_safe()


def test_two_opposing_orders_cannot_hide_unilateral_fill() -> None:
    a = order("buy", "BTC-USDT-SWAP", Side.BUY, "3")
    b = order("sell", "BTC-USDT-SWAP", Side.SELL, "3")
    result = evaluate(orders=[a, b], limits=caps(max_gross=D("0.2")))
    assert not result.allowed
    assert "gross_leverage" in result.breaches
    assert result.tested_scenarios == 9
    assert result.worst_gross == D("300")
    with pytest.raises(ValueError, match="gross_leverage"):
        result.require_safe()


def test_multi_instrument_orders_share_the_same_account_collateral() -> None:
    rows = [state("BTC-USDT-SWAP"), state("ETH-USDT-SWAP")]
    intents = [
        order("btc", "BTC-USDT-SWAP", Side.BUY, "3"),
        order("eth", "ETH-USDT-SWAP", Side.BUY, "3"),
    ]
    result = evaluate(rows, intents, limits=caps(max_gross=D("0.5"), max_instrument_fraction=D("0.5")))
    assert not result.allowed and result.tested_scenarios == 9
    assert result.worst_gross == D("600")
    assert "gross_leverage" in result.breaches


def test_margin_tiers_are_read_from_contract_and_enforced() -> None:
    rows = [OpenPosition(instrument(margin="0.9"), D("0"), D("0"), D("100"), 100)]
    result = evaluate(
        rows,
        [order("highmargin", "BTC-USDT-SWAP", Side.BUY, "4")],
        limits=caps(max_margin_fraction=D("0.3")),
    )
    assert "initial_margin" in result.breaches
    assert result.worst_margin == D("360")


def test_actual_short_stress_and_funding_buffer_can_refuse_exposure() -> None:
    rows = [state(qty="-4", entry="100")]
    limits = caps(
        max_gross=D("2"),
        max_instrument_fraction=D("1"),
        max_loss_currency=D("10"),
        price_gap_bps=D("300"),
        funding_buffer_bps=D("30"),
    )
    result = evaluate(rows, limits=limits)
    assert "monetary_loss" in result.breaches
    assert result.worst_equity < D("990")


def test_existing_marked_profit_is_not_double_counted_when_closing() -> None:
    rows = [state(qty="1", entry="80")]
    result = evaluate(
        rows,
        [order("close", "BTC-USDT-SWAP", Side.SELL, "1", reduce=True)],
        limits=caps(),
    )
    assert result.allowed
    assert result.worst_equity == D("1000")
    assert result.tested_scenarios == 3


def test_adverse_slippage_fees_and_funding_are_not_ignored() -> None:
    limits = caps(
        fee_bps=D("100"),
        slippage_bps=D("100"),
        funding_buffer_bps=D("100"),
        max_loss_currency=D("1"),
    )
    result = evaluate(orders=[order("entry", "BTC-USDT-SWAP", Side.BUY, "1")], limits=limits)
    assert not result.allowed
    assert "monetary_loss" in result.breaches
    assert result.worst_equity < D("998")


def test_pending_cancel_or_partial_fill_is_not_assumed_to_restore_caps() -> None:
    intents = [
        order("open", "BTC-USDT-SWAP", Side.BUY, "3"),
        order("close", "BTC-USDT-SWAP", Side.SELL, "3", reduce=True),
    ]
    # The reduction is not guaranteed to fill; the opening is evaluated independently.
    result = evaluate(orders=intents, limits=caps(max_gross=D("0.2")))
    assert not result.allowed
    assert "gross_leverage" in result.breaches


@pytest.mark.parametrize(
    ("positions", "intents", "currency", "message"),
    [
        ([state("BTC-USDT-SWAP"), state("BTC-USDT-SWAP")], [], "USDT", "duplicate instrument"),
        ([OpenPosition(instrument(currency="USD"), D("0"), D("0"), D("100"), 100)], [], "USDT", "cross-currency"),
        ([state()], [order("id1", "SOL-USDT-SWAP", Side.BUY, "1")], "USDT", "unknown instrument"),
        ([state()], [order("id1", "BTC-USDT-SWAP", Side.BUY, "1")] * 2, "USDT", "duplicate order"),
    ],
)
def test_unqualified_account_inputs_fail_closed(positions, intents, currency, message) -> None:
    with pytest.raises(ValueError, match=message):
        preflight_pending(
            tuple(positions),
            tuple(intents),
            collateral_currency=currency,
            equity=D("1000"),
            peak_equity=D("1000"),
            day_open_equity=D("1000"),
            asof_ns=100,
            ceilings=caps(),
        )


def test_future_and_stale_market_marks_fail_closed() -> None:
    row = state()
    with pytest.raises(ValueError, match="stale"):
        preflight_pending(
            (row,),
            (),
            collateral_currency="USDT",
            equity=D("1000"),
            peak_equity=D("1000"),
            day_open_equity=D("1000"),
            asof_ns=120_000_000_201,
            ceilings=caps(),
        )


def test_bounded_scenario_search_refuses_unchecked_order_count() -> None:
    intents = tuple(order(f"x{i}", "BTC-USDT-SWAP", Side.BUY, "1") for i in range(7))
    with pytest.raises(ValueError, match="too many"):
        evaluate(orders=intents)


def test_unsupported_collateral_and_invalid_caps_do_not_default_to_optimism() -> None:
    with pytest.raises(ValueError, match="peak"):
        evaluate(equity=D("2000"), peak=D("1000"))
    with pytest.raises(ValueError, match="max gross"):
        RiskCeilings(max_gross=D("0"))
    with pytest.raises(TypeError, match="fee_bps"):
        RiskCeilings(fee_bps=0.1)
    with pytest.raises(ValueError, match="gap"):
        RiskCeilings(price_gap_bps=D("6000"))


def test_offline_adapter_assesses_its_real_pending_book_without_order_routing() -> None:
    from lab.markets.adapters import OfflineAdapter
    from lab.markets.domain import MarkPrice
    from lab.markets.simulator import OfflineSimulator

    sim = OfflineSimulator(instrument(), taker_fee_bps=D("0"), slippage_bps=D("0"))
    adapter = OfflineAdapter(sim)
    with pytest.raises(ValueError, match="mark evidence"):
        adapter.preview_pending_risk(asof_ns=100, ceilings=caps())
    sim.on_mark(MarkPrice("mark1", 100, 100, D("100")))
    sim.submit(order("entry", "BTC-USDT-SWAP", Side.BUY, "6"))
    verdict = adapter.preview_pending_risk(asof_ns=100, ceilings=caps())
    assert verdict.tested_scenarios == 3
    assert not verdict.allowed and not verdict.execution_authorized
    assert "gross_leverage" in verdict.breaches
    assert sim.position == 0
    assert len(sim.pending) == 1
    with pytest.raises(ValueError, match="non-rewound"):
        adapter.preview_pending_risk(asof_ns=99, ceilings=caps())
