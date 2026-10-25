"""Fail-closed external-demo reconciliation proof obligations."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.markets.domain import Side, TradingMode
from lab.markets.reconciliation import (
    OrderState,
    ReconciliationSnapshot,
    RecordedOrder,
    reconcile_demo,
)

D = Decimal
ACCOUNT = "a" * 64


def recorded(
    ident: str,
    *,
    qty: str = "2",
    filled: str = "0",
    status: OrderState = OrderState.WORKING,
    symbol: str = "OKX:BTC-USDT-SWAP",
    side: Side = Side.BUY,
) -> RecordedOrder:
    return RecordedOrder(ident, symbol, side, D(qty), D(filled), status)


def snapshot(
    orders: tuple[RecordedOrder, ...] = (),
    positions: tuple[tuple[str, Decimal], ...] = (),
    *,
    mode: TradingMode = TradingMode.BROKER_DEMO,
    observed_ns: int = 100,
    account: str = ACCOUNT,
    balances: tuple[tuple[str, Decimal], ...] = (("USDT", D("1000")),),
) -> ReconciliationSnapshot:
    return ReconciliationSnapshot("OKX", mode, account, observed_ns, orders, positions, balances)


def verify(local, remote):
    return reconcile_demo(local, remote, asof_ns=100)


def test_identical_working_order_and_position_is_consistent_but_not_authorized() -> None:
    local = snapshot((recorded("o1"),), (("OKX:BTC-USDT-SWAP", D("0")),))
    result = verify(local, local)
    assert not result.frozen
    assert result.findings == ()
    assert result.compared_orders == 1
    assert result.execution_authorized is False
    result.require_resolved()


def test_unknown_order_freezes_even_when_broker_agrees_it_is_unknown() -> None:
    unknown = recorded("o1", status=OrderState.UNKNOWN)
    result = verify(snapshot((unknown,)), snapshot((unknown,)))
    assert result.frozen
    assert "unresolved_unknown_order" in result.findings
    with pytest.raises(ValueError, match="unresolved_unknown_order"):
        result.require_resolved()


def test_cancel_fill_race_detected_independently_of_position_agreement() -> None:
    local = snapshot((recorded("order", status=OrderState.CANCELLED),))
    external = snapshot((recorded("order", filled="1", status=OrderState.PARTIAL),))
    result = verify(local, external)
    assert result.frozen
    assert "cumulative_fill_mismatch" in result.findings
    assert "order_lifecycle_mismatch" in result.findings


def test_local_pending_order_missing_from_venue_is_not_assumed_cancelled() -> None:
    result = verify(snapshot((recorded("pending"),)), snapshot())
    assert result.frozen
    assert result.findings == ("unobserved_open_order",)


def test_unowned_external_order_is_never_silently_adopted() -> None:
    result = verify(snapshot(), snapshot((recorded("other"),)))
    assert result.frozen
    assert result.findings == ("unowned_external_order",)


def test_filled_order_missing_terminal_evidence_cannot_hide_behind_net_position() -> None:
    local = snapshot(
        (recorded("filled", filled="2", status=OrderState.FILLED),),
        (("OKX:BTC-USDT-SWAP", D("2")),),
    )
    external = snapshot(positions=(("OKX:BTC-USDT-SWAP", D("2")),))
    result = verify(local, external)
    assert result.frozen
    assert result.findings == ("missing_terminal_fill_evidence",)


def test_flat_cancel_without_fill_can_be_absent_from_open_order_snapshot() -> None:
    local = snapshot((recorded("cancelled", status=OrderState.CANCELLED),))
    assert not verify(local, snapshot()).frozen


@pytest.mark.parametrize(
    "remote",
    [
        snapshot((recorded("id", qty="3"),)),
        snapshot((recorded("id", symbol="OKX:ETH-USDT-SWAP"),)),
        snapshot((recorded("id", side=Side.SELL),)),
    ],
)
def test_order_contract_mismatch_freezes(remote) -> None:
    local = snapshot((recorded("id"),))
    result = verify(local, remote)
    assert result.frozen
    assert "order_identity_or_quantity_conflict" in result.findings


def test_position_mismatch_including_unowned_short_freezes() -> None:
    local = snapshot(positions=(("OKX:BTC-USDT-SWAP", D("2")),))
    for remote in (
        snapshot(positions=(("OKX:BTC-USDT-SWAP", D("1.9")),)),
        snapshot(positions=(("OKX:BTC-USDT-SWAP", D("2")), ("OKX:ETH-USDT-SWAP", D("-1")))),
    ):
        assert "position_mismatch" in verify(local, remote).findings


def test_reconciliation_refuses_wrong_account_mode_venue_or_stale_evidence() -> None:
    local = snapshot()
    with pytest.raises(ValueError, match="modes"):
        verify(local, snapshot(mode=TradingMode.LIVE))
    with pytest.raises(ValueError, match="identity"):
        verify(local, snapshot(account="b" * 64))
    with pytest.raises(ValueError, match="stale"):
        reconcile_demo(local, snapshot(observed_ns=50), asof_ns=100, max_age_ns=10)


def test_duplicate_orders_and_positions_refused_without_silent_netting() -> None:
    one = recorded("dup")
    with pytest.raises(ValueError, match="duplicate external"):
        verify(snapshot(), snapshot((one, one)))
    positions = (("OKX:BTC-USDT-SWAP", D("1")), ("OKX:BTC-USDT-SWAP", D("-1")))
    with pytest.raises(ValueError, match="duplicate local"):
        verify(snapshot(positions=positions), snapshot())


@pytest.mark.parametrize(
    ("filled", "status"),
    [
        ("3", OrderState.WORKING),
        ("0.1", OrderState.WORKING),
        ("0", OrderState.PARTIAL),
        ("2", OrderState.PARTIAL),
        ("1", OrderState.FILLED),
    ],
)
def test_invalid_order_lifecycle_is_rejected_before_reconciliation(filled, status) -> None:
    with pytest.raises(ValueError):
        recorded("bad", filled=filled, status=status)


def test_mutating_a_snapshot_cannot_override_paper_only_guard() -> None:
    demo = snapshot()
    with pytest.raises(ValueError, match="modes"):
        verify(replace(demo, mode=TradingMode.OFFLINE_SIM), demo)


def test_cash_collateral_currency_and_amounts_must_match_exactly() -> None:
    baseline = snapshot()
    assert verify(baseline, baseline).compared_currencies == 1
    for remote in (
        snapshot(balances=(("USDT", D("999.99")),)),
        snapshot(balances=(("USD", D("1000")),)),
        snapshot(balances=(("USDT", D("1000")), ("USD", D("0")))),
    ):
        result = verify(baseline, remote)
        assert result.frozen
        assert "collateral_balance_mismatch" in result.findings


def test_duplicate_collateral_currency_and_empty_broker_balance_are_refused() -> None:
    with pytest.raises(ValueError, match="collateral"):
        snapshot(balances=())
    duplicate = snapshot(balances=(("USDT", D("1")), ("USDT", D("2"))))
    with pytest.raises(ValueError, match="duplicate"):
        verify(duplicate, snapshot())
