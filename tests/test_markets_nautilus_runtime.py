"""Real Nautilus backtest engine, offline-only data and venue sanity checks."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.markets.domain import AssetClass, ContractKind, Instrument, MarkPrice, Side, Trade
from lab.markets.nautilus_data import CausalNautilusTape, build_causal_tape
from lab.markets.nautilus_instruments import LinearContractDefinition
from lab.markets.nautilus_runtime import replay_native_observations

D = Decimal


def spec():
    contract = Instrument(
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
    )
    return LinearContractDefinition(
        contract,
        "BTC",
        "USD",
        D("2"),
        D("5"),
        0,
        0,
        "offline_fixture",
    )


def tape():
    return build_causal_tape(
        spec().instrument,
        (
            MarkPrice("m1", 1_000_000_000, 1_000_000_001, D("100.1234")),
            Trade("t1", 2_000_000_000, 2_000_000_001, D("100.1"), D("0.010"), Side.BUY),
            MarkPrice("m2", 3_000_000_000, 3_000_000_001, D("100.2234")),
            Trade("t2", 4_000_000_000, 4_000_000_001, D("100.2"), D("0.020"), Side.SELL),
        ),
    )


def test_real_native_engine_delivers_independent_trade_and_mark_observations():
    result = replay_native_observations(spec(), tape(), collateral=D("1000"))
    assert result.native_data_engine_used
    assert result.event_integrity_verified
    assert result.clock_receipt_verified
    assert result.native_instrument_id == "BTC-USD-SWAP.SIM"
    assert result.trade_observations == 2
    assert result.mark_observations == 2
    assert result.first_received_ns == 1_000_000_001
    assert result.last_received_ns == 4_000_000_001
    assert result.maker_fee_rate == D("0.0002")
    assert result.taker_fee_rate == D("0.0005")
    assert not result.broker_execution_enabled
    assert not result.fees_charged_verified
    assert not result.strategies_qualified


def test_real_native_replay_is_deterministic_and_does_not_create_orders():
    first = replay_native_observations(spec(), tape())
    second = replay_native_observations(spec(), tape())
    assert first == second


@pytest.mark.parametrize(
    ("modification", "reason"),
    [
        ({"metadata_source": "public_read_only"}, "unqualified external"),
        ({"maker_fee_bps": D("NaN")}, "nonfinite"),
    ],
)
def test_unqualified_specification_refuses_before_engine_creation(modification, reason):
    with pytest.raises(ValueError, match=reason):
        candidate = replace(spec(), **modification)
        replay_native_observations(candidate, tape())


def test_contract_metadata_cannot_be_observed_in_the_future_of_replayed_events():
    observed = tape()
    first_receipt = observed.events[0].ts_init
    late_contract = replace(spec(), observed_ns=first_receipt + 1)
    with pytest.raises(ValueError, match="metadata was received after"):
        replay_native_observations(late_contract, observed)


def test_wrong_native_contract_or_tape_claim_fails_closed():
    observed = tape()
    other = replace(spec(), instrument=replace(spec().instrument, venue="OKX"))
    with pytest.raises(ValueError, match="identity"):
        replay_native_observations(other, observed)
    with pytest.raises(ValueError, match="qualified"):
        replay_native_observations(
            spec(),
            replace(observed, execution_qualified=True),
        )
    with pytest.raises(ValueError, match="terminal"):
        replay_native_observations(
            spec(),
            replace(observed, last_received_ns=5_000_000_000),
        )
    with pytest.raises(ValueError, match="nonempty"):
        replay_native_observations(spec(), replace(observed, events=()))


def test_risk_collateral_and_event_ownership_are_checked_before_native_engine():
    with pytest.raises(TypeError, match="exact Decimal"):
        replay_native_observations(spec(), tape(), collateral=1000.0)
    with pytest.raises(ValueError, match="positive"):
        replay_native_observations(spec(), tape(), collateral=D("-10"))
    original = tape()
    misbound = replace(original, events=(original.events[2], original.events[0]))
    with pytest.raises(ValueError, match="noncausal"):
        replay_native_observations(spec(), misbound)


def test_unbounded_tape_does_not_allocate_an_engine():
    original = tape()
    large = CausalNautilusTape(
        original.instrument_id,
        (original.events[0],) * 10_001,
        original.last_received_ns,
    )
    with pytest.raises(ValueError, match="bounded"):
        replay_native_observations(spec(), large)
