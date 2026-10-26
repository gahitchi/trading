"""Native F1 experiment: an independent writeoff must NOT hide open Nautilus positions.

This uses the actual BacktestEngine, Portfolio, Cache and Account on a wholly
synthetic fixture. A legitimate corporate-action position/cash adjustment is
not available or assumed; fail-closed is the *expected* result. No broker.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    Currency,
    Money,
    OmsType,
    OrderSide,
    Quantity,
    TimeInForce,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.markets.__main__ import fixture_instrument
from lab.markets.corporate_actions import CashTermination, CorporateBook, ReconciledBook, recognize
from lab.markets.native_corporate_parity import (
    NativeCorporateParityError,
    read_native_corporate_state,
    require_native_settlement_parity,
)
from lab.markets.native_fill_parity import _RoundTripConfig, _RoundTripProbe, synthetic_quotes
from lab.markets.nautilus_instruments import LinearContractDefinition, to_nautilus_perpetual

HASH = "a" * 64


class _OpenOnlyConfig(StrategyConfig):
    def __init__(self, *, instrument_id, side):
        super().__init__()
        self.instrument_id = instrument_id
        self.side = side


class _OpenOnly(Strategy):
    """Open one actual native position; never submit a closing market order."""

    def __init__(self, config):
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.side = config.side
        self.seen = 0
        self.fills = []

    def on_start(self):
        self.subscribe_quotes(self.instrument_id)

    def on_quote(self, quote):
        self.seen += 1
        if self.seen == 2:
            order = self.order_factory.market(
                self.instrument_id, self.side, Quantity.from_str("1.000"), time_in_force=TimeInForce.GTC
            )
            self.submit_order(order)

    def on_order_filled(self, event):
        self.fills.append(event)


def _fixture(side, *, close=False):
    instrument = replace(fixture_instrument(), symbol="BTC-USD-SWAP")
    spec = LinearContractDefinition(
        instrument=instrument,
        base_currency="BTC",
        settlement_currency="USD",
        maker_fee_bps=Decimal("2"),
        taker_fee_bps=Decimal("5"),
        definition_ns=0,
        observed_ns=0,
        metadata_source="offline_fixture",
    )
    native = to_nautilus_perpetual(spec)
    venue = Venue(instrument.venue)
    currency = Currency.from_str(instrument.quote_currency)
    strategy = (
        _RoundTripProbe(_RoundTripConfig(instrument_id=native.id, quantity=Quantity.from_str("1.000"), entry_side=side))
        if close
        else _OpenOnly(_OpenOnlyConfig(instrument_id=native.id, side=side))
    )
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money.from_decimal(Decimal("1000.00"), currency)],
        base_currency=currency,
        default_leverage=Decimal("1"),
        fee_model=MakerTakerFeeModel(
            maker_rate=spec.maker_fee_bps / Decimal("10000"),
            taker_rate=spec.taker_fee_bps / Decimal("10000"),
        ),
    )
    engine.add_instrument(native)
    engine.add_strategy(strategy)
    engine.add_data(list(synthetic_quotes(spec)))
    return engine, strategy, native, venue, currency


def _independent_writeoff(native, side):
    position = Decimal("1.000") if side == OrderSide.BUY else Decimal("-1.000")
    basis = Decimal("100.20") if side == OrderSide.BUY else Decimal("-100.00")
    book = CorporateBook(
        instrument_id=str(native.id),
        currency="USD",
        quantum=Decimal("0.01"),
        position=position,
        signed_basis=basis,
        cash=Decimal("1000.00"),
    )
    event = CashTermination(
        action_id="WRITE_OFF_1",
        instrument_id=str(native.id),
        currency="USD",
        kind="bankruptcy_writeoff",
        cash_per_unit=Decimal("0"),
        effective_ns=1,
        available_ns=1,
        payable_not_before_ns=1,
        source_id="synthetic_offline",
        source_sha256=HASH,
        action_version="fixture_v1",
    )
    recon = ReconciledBook(
        position=position,
        positions_verified=True,
        orders_verified=True,
        open_order_ids=(),
        action_version="fixture_v1",
    )
    return recognize(book, event, recon, observed_ns=4_000_000_001)


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_native_position_remains_open_when_only_python_book_is_retired(side):
    engine, probe, instrument, venue, currency = _fixture(side)
    try:
        engine.run()
        assert probe.seen == 4 and len(probe.fills) == 1
        observed = read_native_corporate_state(engine, instrument_id=instrument.id, venue=venue, currency=currency)
        assert observed.fill_count == 1
        assert observed.account_total.is_finite()
        assert not observed.position_flat
        assert observed.cache_open_positions >= 1
        assert observed.native_engine_readback
        assert not observed.native_corporate_action_transition_verified
        independent = _independent_writeoff(instrument, side)
        assert independent.position == 0
        with pytest.raises(NativeCorporateParityError, match="still contains an open"):
            require_native_settlement_parity(independent, observed, baseline_fill_count=1)
        # No synthetic bar or settlement market order was injected to close it.
        assert len(engine.generate_fills_report()) == 1
    finally:
        engine.dispose()


def test_market_order_close_cannot_be_disguised_as_nonfill_settlement():
    engine, probe, instrument, venue, currency = _fixture(OrderSide.BUY, close=True)
    try:
        engine.run()
        assert probe.quotes_seen == 4 and len(probe.fills) == 2
        observed = read_native_corporate_state(engine, instrument_id=instrument.id, venue=venue, currency=currency)
        assert observed.position_flat and observed.cache_open_positions == 0
        assert observed.cache_open_orders == 0
        assert observed.fill_count == 2
        independent = _independent_writeoff(instrument, OrderSide.BUY)
        with pytest.raises(NativeCorporateParityError, match="new native market fills"):
            require_native_settlement_parity(independent, observed, baseline_fill_count=1)
    finally:
        engine.dispose()
