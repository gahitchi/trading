"""Real pinned Nautilus L2 matching of deliberately synthetic partial fills."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.markets.domain import AssetClass, ContractKind, Instrument
from lab.markets.native_l2_parity import NativeL2Error, run_native_l2_partial_parity
from lab.markets.nautilus_instruments import LinearContractDefinition

D = Decimal


def spec():
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
        ),
        base_currency="BTC",
        settlement_currency="USD",
        maker_fee_bps=D("2"),
        taker_fee_bps=D("5"),
        definition_ns=0,
        observed_ns=0,
        metadata_source="offline_fixture",
    )


def test_native_l2_partial_order_round_trip_charges_fees_and_settles_actual_account():
    observed = run_native_l2_partial_parity(spec())
    assert observed.fill_count == 3
    assert observed.entry_parts == (D("0.400"), D("0.600"))
    assert observed.entry_prices == (D("100.2"), D("100.3"))
    assert observed.realized_pnl == D("0.7400")
    assert observed.charged_fees == D("0.08")
    assert observed.ending_balance == D("1000.66")
    assert observed.native_account_parity_verified
    assert not observed.broker_execution_enabled
    assert not observed.market_data_qualified
    assert not observed.actual_queue_priority_verified


def test_native_l2_partial_scenario_does_not_accept_external_or_misbound_contract():
    with pytest.raises(NativeL2Error, match="artificial"):
        run_native_l2_partial_parity(replace(spec(), metadata_source="public_read_only"))
    with pytest.raises(NativeL2Error, match="artificial"):
        run_native_l2_partial_parity(replace(spec(), instrument=replace(spec().instrument, venue="OKX")))
    with pytest.raises(NativeL2Error, match="exact"):
        run_native_l2_partial_parity(spec(), collateral=D("1000.001"))
