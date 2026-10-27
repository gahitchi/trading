"""F1.5 partial fill/order-status parity without an execution path."""

import json
from decimal import Decimal

import pytest

from lab.engine_events import BrokerFillFact, EngineEconomicBook, EngineFactError, FillFact
from lab.order_observation import (
    parse_external_order_statement,
    reconcile_external_orders,
)


def D(value):
    return Decimal(value)


def book(*, simulated=False):
    account = EngineEconomicBook("SIM-001")
    if simulated:
        return account.append(FillFact("sim", "sim:fill", 1, "SPY", "BUY", D("1"), D("100"), D("0")))
    return account.append(
        BrokerFillFact(
            "receipt-1",
            "broker:r1",
            1,
            "order-1",
            "SPY",
            "BUY",
            D("0.25"),
            D("100"),
            D("0.01"),
            "a" * 64,
        )
    ).append(
        BrokerFillFact(
            "receipt-2",
            "broker:r2",
            2,
            "order-1",
            "SPY",
            "BUY",
            D("0.75"),
            D("100.1"),
            D("0.02"),
            "b" * 64,
        )
    )


def order(*, order_id="order-1", status="CANCELED", requested="2", executed="1", remaining="0"):
    return {
        "broker_order_id": order_id,
        "symbol": "SPY",
        "side": "BUY",
        "status": status,
        "requested_quantity": requested,
        "executed_quantity": executed,
        "remaining_quantity": remaining,
    }


def report_bytes(*, orders=None, as_of_ns=3, complete=True, account_id="SIM-001"):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": account_id,
            "record_id": "order-report-01",
            "as_of_ns": as_of_ns,
            "source_kind": "BROKER_ORDER_STATEMENT",
            "complete_order_scope": complete,
            "orders": orders if orders is not None else [order()],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def parse(raw):
    return parse_external_order_statement(raw, source_namespace="broker", expected_account_id="SIM-001")


def test_partial_fills_then_cancelled_remainder_agree_with_complete_broker_summary():
    current = book()
    observed = parse(report_bytes())
    result = reconcile_external_orders(current, observed)
    assert result.matches
    assert result.broker_fills_checked == 2
    assert result.journal_evidence_head == current.evidence_head
    assert len(result.statement_sha256) == 64
    assert len(result.order_deltas) == 1
    assert result.order_deltas[0].delta == 0
    assert result.order_deltas[0].report_quantity == D("1")
    assert result.order_deltas[0].custody_quantity == D("1")


def test_missing_reported_execution_yields_nonzero_discrepancy_not_fake_fill():
    current = book()
    observed = parse(report_bytes(orders=[order(status="CANCELED", executed="1.25")]))
    result = reconcile_external_orders(current, observed)
    assert not result.matches
    assert result.order_deltas[0].delta == D("0.25")
    assert len(current.events) == 2
    assert current.ledger.position("SPY") == D("1")


def test_external_extra_zero_execution_order_is_valid_scope():
    current = book()
    observed = parse(
        report_bytes(
            orders=[
                order(),
                order(order_id="order-2", status="REJECTED", requested="10", executed="0"),
                order(order_id="order-3", status="ACCEPTED", requested="10", executed="0", remaining="10"),
            ]
        )
    )
    result = reconcile_external_orders(current, observed)
    assert result.matches
    assert len(result.order_deltas) == 3


def test_broker_receipt_missing_from_claimed_complete_order_scope_is_hard_error():
    observed = parse(report_bytes(orders=[]))
    with pytest.raises(EngineFactError, match="absent"):
        reconcile_external_orders(book(), observed)


def test_simulator_fill_cannot_establish_external_broker_order_parity():
    with pytest.raises(EngineFactError, match="simulator"):
        reconcile_external_orders(book(simulated=True), parse(report_bytes(orders=[])))


@pytest.mark.parametrize(
    "observed_order",
    [
        order(status="FILLED", requested="2", executed="1"),
        order(status="FILLED", requested="2", executed="2", remaining="1"),
        order(status="CANCELED", requested="2", executed="2"),
        order(status="PARTIALLY_FILLED", requested="2", executed="0", remaining="2"),
        order(status="PARTIALLY_FILLED", requested="2", executed="1", remaining="2"),
        order(status="ACCEPTED", requested="2", executed="1", remaining="1"),
        order(status="REJECTED", requested="2", executed="1"),
        order(status="CANCEL_PENDING", requested="2", executed="1"),
        order(status="CANCELED", requested="2", executed="-1"),
        order(status="CANCELED", requested="2", executed="2.1"),
        order(status="CANCELED", requested="2", executed=1.0),
        order(status="CANCELED", requested="2", executed="NaN"),
        order(status="CANCELED", requested="2", executed="1E+999999999"),
        order(status="CANCELED", requested="2", remaining="1"),
        order(status="CANCELED", requested="0", executed="0"),
    ],
)
def test_invalid_order_status_and_quantity_combinations_are_refused(observed_order):
    with pytest.raises(EngineFactError):
        parse(report_bytes(orders=[observed_order]))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"garbage",
        b"[]",
        b'{"orders":[],"orders":[]}',
        b'{"as_of_ns":NaN}',
        b"\xff",
        b"x" * (128 * 1024 + 1),
    ],
)
def test_external_order_observation_refuses_bad_bounded_wire_documents(raw):
    with pytest.raises(EngineFactError):
        parse(raw)


def test_duplicate_order_id_is_refused():
    with pytest.raises(EngineFactError, match="duplicate"):
        parse(report_bytes(orders=[order(), order()]))


def test_missing_account_scope_or_stale_asof_does_not_establish_parity():
    with pytest.raises(EngineFactError):
        parse(report_bytes(complete=False))
    with pytest.raises(EngineFactError):
        parse(report_bytes(account_id="OTHER"))
    with pytest.raises(EngineFactError, match="stale"):
        reconcile_external_orders(book(), parse(report_bytes(as_of_ns=1)))


def test_mismatched_instrument_or_side_is_not_silently_netting():
    changed = order()
    changed["symbol"] = "QQQ"
    with pytest.raises(EngineFactError, match="instrument"):
        reconcile_external_orders(book(), parse(report_bytes(orders=[changed])))
    changed = order()
    changed["side"] = "SELL"
    with pytest.raises(EngineFactError, match="side"):
        reconcile_external_orders(book(), parse(report_bytes(orders=[changed])))


def test_extra_broker_reported_quantity_cannot_mask_missing_fill_event():
    current = book()
    observed = parse(report_bytes(orders=[order(status="FILLED", requested="2", executed="2")]))
    result = reconcile_external_orders(current, observed)
    assert not result.matches
    assert result.order_deltas[0].delta == D("1")


def test_empty_broker_order_book_matches_empty_engine_account():
    report = parse(report_bytes(orders=[]))
    parity = reconcile_external_orders(EngineEconomicBook("SIM-001"), report)
    assert parity.matches
    assert parity.broker_fills_checked == 0
