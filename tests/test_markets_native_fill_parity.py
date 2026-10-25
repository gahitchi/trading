"""Pinned Nautilus 2.0.0rc7: prove synthetic native fills really cost money."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from nautilus_trader.model import Price, Quantity, QuoteTick

from lab.markets.domain import AssetClass, ContractKind, Instrument, Side
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
    for side in (Side.BUY, Side.SELL):
        left = run_native_fill_parity(spec(), synthetic_quotes(spec()), side=side)
        right = run_native_fill_parity(spec(), synthetic_quotes(spec()), side=side)
        assert left == right


def test_short_cover_reconciles_negative_pnl_and_both_taker_fees():
    s = spec()
    result = run_native_fill_parity(s, synthetic_quotes(s), side=Side.SELL)
    assert result.entry_side == Side.SELL
    assert result.fills == 2
    assert result.entry_price == D("100.0")
    assert result.exit_price == D("101.2")
    assert result.quantity == D("1.000")
    assert result.gross_pnl == D("-1.2")
    assert result.charged_fees == D("0.10")
    assert result.net_pnl == D("-1.30")
    assert result.ending_balance == D("998.70")
    assert result.ending_balance == result.expected_ending_balance
    assert result.execution_cost_parity_verified
    assert not result.broker_execution_enabled


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


def test_modified_but_causal_quotes_cannot_be_claimed_as_the_synthetic_fixture():
    s = spec()
    q = synthetic_quotes(s)
    forged = QuoteTick(
        q[1].instrument_id,
        Price.from_str("99.9"),
        Price.from_str("100.2"),
        Quantity.from_str("10.000"),
        Quantity.from_str("10.000"),
        q[1].ts_event,
        q[1].ts_init,
    )
    with pytest.raises(NativeParityError, match="exact fixed synthetic"):
        run_native_fill_parity(s, (q[0], forged, q[2], q[3]))
    with pytest.raises(TypeError, match="typed Side"):
        run_native_fill_parity(s, q, side="sell")


def test_synthetic_test_feed_must_not_be_mislabelled_as_real_market_data():
    s = spec()
    quotes = synthetic_quotes(s)
    assert len(quotes) == 4
    assert quotes[1].bid_price != quotes[1].ask_price
    assert quotes[2].ts_init > quotes[1].ts_init
    result = run_native_fill_parity(s, quotes)
    assert result.source == "synthetic_offline_quotes"


def test_cli_outputs_native_fees_and_never_authorizes_a_broker():
    import json
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "lab.markets", "native-fill-parity"],
        capture_output=True,
        text=True,
        check=True,
        timeout=40,
    )
    payload = json.loads(completed.stdout)
    assert payload["fills"] == 2
    assert payload["sides_verified"] == ["buy", "sell"]
    assert payload["charged_fees"] == "0.10"
    short = payload["short_round_trip"]
    assert short["fills"] == 2
    assert short["gross_pnl"] == "-1.2000"
    assert short["charged_fees"] == "0.10"
    assert short["ending_balance"] == "998.70"
    assert short["ending_balance"] == short["expected_ending_balance"]
    assert payload["ending_balance"] == payload["expected_ending_balance"]
    assert payload["execution_cost_parity_verified"] is True
    assert payload["input_kind"] == "synthetic_offline_quotes"
    assert payload["broker_execution_enabled"] is False
    assert payload["qualified_for_live_trading"] is False
