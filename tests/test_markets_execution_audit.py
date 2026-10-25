"""Adversarial partial-fill, fee and signed funding oracle regressions."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.markets.domain import AssetClass, ContractKind, Instrument, Side
from lab.markets.execution_audit import (
    AuditMismatch,
    AuditOrder,
    RecordedFill,
    RecordedFunding,
    audit_observed_cashflows,
)

D = Decimal


def contract():
    return Instrument(
        "SIM",
        "BTC-USD-SWAP",
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USD",
        D("0.1"),
        D("0.001"),
        D("0.001"),
        D("5"),
    )


def plans():
    return (AuditOrder("entry", Side.BUY, D("1.000")), AuditOrder("exit", Side.SELL, D("1.000")))


def first():
    return RecordedFill("entry", "fill1", Side.BUY, D("0.400"), D("100.2"), D("0.02"), 1_000_000_000)


def second():
    return RecordedFill("entry", "fill2", Side.BUY, D("0.600"), D("100.2"), D("0.03"), 2_000_000_000)


def close():
    return RecordedFill("exit", "fill3", Side.SELL, D("1.000"), D("101.0"), D("0.05"), 4_000_000_000)


def funding():
    return RecordedFunding(
        "fund1",
        3_000_000_000,
        3_000_000_000,
        2_500_000_000,
        D("100"),
        D("0.01"),
        D("1.00"),
    )


def run(events, orders=None):
    return audit_observed_cashflows(
        contract(),
        plans() if orders is None else orders,
        events,
        collateral=D("1000"),
        taker_fee_bps=D("5"),
    )


def test_two_partial_fills_and_funding_between_entry_and_exit():
    observed = run((first(), second(), funding(), close()))
    assert observed.fill_count == 3 and observed.funding_count == 1
    assert observed.final_position == 0
    assert observed.final_entry_price == 0
    assert observed.realized_pnl == D("0.8000")
    assert observed.charged_fees == D("0.10")
    assert observed.funding_debits == D("1.00")
    assert observed.ending_balance == D("999.70")
    assert observed.remaining_orders == (("entry", D("0.000")), ("exit", D("0.000")))
    assert not observed.execution_authorized
    assert not observed.broker_account_reconciled


def test_partial_fill_keeps_working_quantity_and_uses_actual_position_at_funding():
    observed = run((first(), replace(funding(), reported_debit=D("0.40"))))
    assert observed.final_position == D("0.400")
    assert observed.remaining_orders == (("entry", D("0.600")), ("exit", D("1.000")))
    assert observed.ending_balance == D("999.58")


def test_short_funding_credit_and_cover():
    sell = AuditOrder("short", Side.SELL, D("1"))
    buy = AuditOrder("cover", Side.BUY, D("1"))
    events = (
        RecordedFill("short", "s1", Side.SELL, D("1"), D("100.0"), D("0.05"), 100),
        RecordedFunding("f1", 200, 200, 190, D("100"), D("0.01"), D("-1.00")),
        RecordedFill("cover", "c1", Side.BUY, D("1"), D("101.2"), D("0.05"), 300),
    )
    result = run(events, (sell, buy))
    assert result.realized_pnl == D("-1.2")
    assert result.funding_debits == D("-1")
    assert result.ending_balance == D("999.70")


@pytest.mark.parametrize(
    "replacement,reason",
    [
        (replace(first(), quantity=D("1.100")), "remaining"),
        (replace(first(), quantity=D("0.4005")), "step"),
        (replace(first(), charged_fee=D("0.01")), "taker fee"),
        (replace(first(), charged_fee=D("0.0201")), "taker fee"),
        (replace(first(), side=Side.SELL), "side"),
        (replace(first(), client_id="unknown"), "unowned"),
    ],
)
def test_wrong_fill_accounting_never_passes(replacement, reason):
    with pytest.raises(AuditMismatch, match=reason):
        run((replacement,))


def test_duplicate_fills_and_future_funding_refused():
    with pytest.raises(AuditMismatch, match="duplicated trade"):
        run((first(), first()))
    with pytest.raises(AuditMismatch, match="funding mark stale"):
        run((first(), replace(funding(), mark_ns=3_000_000_001)))
    with pytest.raises(AuditMismatch, match="funding cashflow"):
        run((first(), replace(funding(), reported_debit=D("0.00"))))
    with pytest.raises(AuditMismatch, match="funding source"):
        run((first(), replace(funding(), source="OKX_EEA_PUBLIC_INDICATIVE_FUNDING")))
    with pytest.raises(AuditMismatch, match="receipt order"):
        run((second(), first()))
    with pytest.raises(AuditMismatch, match="event time regressed"):
        delayed = replace(funding(), event_ns=500_000_000, mark_ns=400_000_000, reported_debit=D("0.40"))
        run((first(), delayed))
    with pytest.raises(AuditMismatch, match="duplicated funding"):
        once = replace(funding(), reported_debit=D("0.40"))
        twice = replace(once, observed_ns=3_000_000_001)
        run((first(), once, twice))


def test_native_opaque_order_ids_are_bound_but_not_used_for_local_order_creation():
    native = AuditOrder("O-19700101-000000-001", Side.BUY, D("1"))
    fill = replace(
        first(),
        client_id=native.client_id,
        trade_id="T-19700101-001",
        quantity=D("1"),
        charged_fee=D("0.05"),
    )
    result = run((fill,), (native,))
    assert result.fill_count == 1
    assert result.remaining_orders == ((native.client_id, D("0")),)
    with pytest.raises(AuditMismatch, match="durable order identity"):
        run((), (replace(native, client_id="../secrets"),))


def test_unqualified_spot_and_noncrypto_contracts_cannot_use_linear_perpetual_math():
    spot = replace(contract(), kind=ContractKind.SPOT)
    equity = Instrument(
        "SIM",
        "BTC",
        AssetClass.EQUITY,
        ContractKind.SHARE,
        "USD",
        D("0.01"),
        D("1"),
        D("1"),
        D("1"),
    )
    for unsupported in (spot, equity):
        with pytest.raises(ValueError, match="crypto linear"):
            audit_observed_cashflows(unsupported, (), (), collateral=D("1000"), taker_fee_bps=D("5"))


def test_bad_collateral_precision_and_unsupported_inputs():
    with pytest.raises(AuditMismatch, match="representable"):
        audit_observed_cashflows(contract(), plans(), (), collateral=D("1000.001"), taker_fee_bps=D("5"))
    with pytest.raises(TypeError, match="exact Decimal"):
        audit_observed_cashflows(contract(), plans(), (), collateral=1000.0, taker_fee_bps=D("5"))
    with pytest.raises(AuditMismatch, match="duplicate durable"):
        run((), (plans()[0], plans()[0]))
