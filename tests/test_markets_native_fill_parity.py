"""Pinned Nautilus 2.0.0rc7: prove synthetic native fills really cost money."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from nautilus_trader.model import Price, Quantity, QuoteTick

from lab.markets.domain import AssetClass, ContractKind, Instrument
from lab.markets.native_fill_parity import NativeParityError, run_native_fill_parity, synthetic_quotes
from lab.markets.nautilus_instruments import LinearContractDefinition

D = Decimal


def spec() -> LinearContractDefinition:
    return LinearContractDefinition(
        instrument=Instrument(
            "SIM",
            "BTC-USD-SWAP",
            AssetClass.CRYPTO,
            ContractKind.LINEAR_DERIVATIVE,
            "USD",
            D("0.1"),
            D("0.001"),
            D("0.001"),
            D("5"),
            multiplier=D("1"),
        ),
        base_currency="BTC",
        settlement_currency="USD",
        maker_fee_bps=D("2"),
        taker_fee_bps=D("5"),
        definition_ns=0,
        observed_ns=0,
        metadata_source="offline_fixture",
    )


def test_real_native_round_trip_charges_both_taker_fees_and_reconciles_account():
    result = run_native_fill_parity(spec(), synthetic_quotes(spec()))
    assert result.fills == 2
    assert result.entry_price == D("100.2")
    assert result.exit_price == D("101.0")
    assert result.quantity == D("1")
    assert result.gross_pnl == D("0.8")
    assert result.charged_fees == D("0.10")  # USD has two decimal places
    assert result.net_pnl == D("0.70")
    assert result.ending_balance == D("1000.70")
    assert result.ending_balance == result.expected_ending_balance
    assert result.execution_cost_parity_verified
    assert not result.broker_execution_enabled
    assert not result.qualified_for_live_trading


def test_real_native_parity_is_reproducible_without_a_broker():
    left = run_native_fill_parity(spec(), synthetic_quotes(spec()))
    right = run_native_fill_parity(spec(), synthetic_quotes(spec()))
    assert left == right


def test_unqualified_metadata_and_zero_fee_are_rejected():
    s = spec()
    q = synthetic_quotes(s)
    with pytest.raises(NativeParityError, match="offline_fixture"):
        run_native_fill_parity(replace(s, metadata_source="public_read_only"), q)
    with pytest.raises(NativeParityError, match="positive taker fee"):
        run_native_fill_parity(replace(s, taker_fee_bps=D("0")), q)
    with pytest.raises(NativeParityError, match="metadata observed after"):
        run_native_fill_parity(replace(s, observed_ns=q[0].ts_init + 1), q)


def test_invalid_collateral_quantity_and_feed_refused_before_native_engine():
    s = spec()
    q = synthetic_quotes(s)
    with pytest.raises(TypeError, match="exact Decimal"):
        run_native_fill_parity(s, q, quantity=1.0)
    with pytest.raises(ValueError, match="positive"):
        run_native_fill_parity(s, q, collateral=D("-1"))
    with pytest.raises(ValueError, match="minimum"):
        run_native_fill_parity(s, q, quantity=D("0.0005"))
    with pytest.raises(NativeParityError, match="exactly four"):
        run_native_fill_parity(s, q[:3])
    bad = QuoteTick(
        q[2].instrument_id,
        Price.from_str("101.0"),
        Price.from_str("101.2"),
        Quantity.from_str("0.100"),
        Quantity.from_str("0.100"),
        q[2].ts_event,
        q[2].ts_init,
    )
    with pytest.raises(NativeParityError, match="insufficient"):
        run_native_fill_parity(s, (q[0], q[1], bad, q[3]))


def test_synthetic_test_feed_must_not_be_mislabelled_as_real_market_data():
    s = spec()
    quotes = synthetic_quotes(s)
    assert len(quotes) == 4
    assert quotes[1].bid_price != quotes[1].ask_price
    assert quotes[2].ts_init > quotes[1].ts_init
    result = run_native_fill_parity(s, quotes)
    assert result.source == "synthetic_offline_quotes"
