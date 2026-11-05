"""Exact money, funding/cost, mark-to-market and UTC risk-budget invariants."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from lab.profit_allocation import AllocationEvent, AllocationPolicy, ProfitAllocationLedger


D = Decimal
START = date(2026, 11, 5)
T0 = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)


def policy(**kwargs):
    fields = dict(currency="USD", initial_capital="1000.00", quantum="0.01", reinvest_fraction="0.5", max_deployable="1500.00")
    return AllocationPolicy(**(fields | kwargs))


def ledger(**kwargs):
    return ProfitAllocationLedger(policy(**kwargs), START)


def event(i, kind, amount, *, minutes=1, **kwargs):
    return AllocationEvent(
        event_id=f"event:{i}", source_id=f"source:{i}", timestamp=T0 + timedelta(minutes=minutes),
        currency="USD", kind=kind, amount=amount, **kwargs,
    )


def trade(i, net, *, gross=None, fees="0.00", slippage="0.00", funding="0.00", released_mark="0.00", minutes=1):
    return event(i, "trade", net, minutes=minutes, gross=net if gross is None else gross,
                 fees=fees, slippage=slippage, funding=funding, released_mark=released_mark)


def close(i, day, mark="0.00"):
    return AllocationEvent(
        f"event:{i}", f"source:{i}", datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC),
        "USD", "day_close", mark,
    )


@pytest.mark.parametrize("bad", [float("nan"), 0.5, True, "NaN", "Infinity", "-0.01", "1.01", "not a number"])
def test_invalid_fraction_rejected(bad):
    with pytest.raises(ValueError):
        policy(reinvest_fraction=bad)


@pytest.mark.parametrize("bad", ["0", "0.03", "-0.01", "NaN", "1.5", 0.01])
def test_invalid_money_quantum_rejected(bad):
    with pytest.raises(ValueError):
        policy(quantum=bad)


def test_variable_daily_profit_loss_and_fixed_capital_reinvestment():
    l = ledger()
    l.apply(trade("small", "0.20"))
    one = l.apply(close("d1", START))
    assert one.net_pnl == D("0.20")
    assert one.deployable == D("1000.10") and one.reserve == D("0.10")
    l.apply(trade("large", "120.00", minutes=24 * 60 + 1))
    two = l.apply(close("d2", START + timedelta(days=1)))
    assert two.net_pnl == D("120.00")
    assert two.deployable == D("1060.10") and two.reserve == D("60.10")
    l.apply(trade("loss", "-80.00", minutes=2 * 24 * 60 + 1))
    three = l.apply(close("d3", START + timedelta(days=2)))
    assert three.net_pnl == D("-80.00")
    assert three.deployable == D("980.10") and three.reserve == D("60.10")
    assert l.equity == D("1040.20")
    assert l.risk_capacity == D("980.10")
    report = l.report(START + timedelta(days=2))
    assert report["net_pnl"] == "40.20"
    assert (report["winning_days"], report["losing_days"], report["flat_days"]) == (2, 1, 0)
    assert report["max_drawdown_currency"] == "80.00"


def test_no_default_risk_permission_when_cap_is_absent():
    l = ledger(max_deployable=None)
    assert l.risk_capacity is None
    l.apply(trade("win", "1200.00"))
    assert l.risk_capacity is None
    assert l.equity == D("2200.00")


def test_configured_cap_limits_deployable_even_after_repeated_wins():
    l = ledger(max_deployable="1020.00")
    l.apply(trade("w1", "120.00"))
    assert l.deployable == D("1060.00")
    assert l.risk_capacity == D("1020.00")
    l.apply(trade("w2", "800.00", minutes=2))
    assert l.risk_capacity == D("1020.00")
    l.apply(trade("l1", "-1500.00", minutes=3))
    assert l.deployable == D("-40.00")
    assert l.risk_capacity == D("0")


@pytest.mark.parametrize("fraction,expected_deployable,expected_reserve", [
    ("0", "1000.00", "10.01"), ("1", "1010.01", "0.00"), ("0.333333", "1003.33", "6.68")
])
def test_rounding_conserves_every_cent(fraction, expected_deployable, expected_reserve):
    l = ledger(reinvest_fraction=fraction)
    l.apply(trade("win", "10.01"))
    assert l.deployable == D(expected_deployable)
    assert l.reserve == D(expected_reserve)
    assert l.equity == D("1010.01")


def test_trade_costs_are_reconciled_and_funding_not_double_counted():
    l = ledger()
    l.apply(trade("audited", "120.00", gross="121.00", fees="0.40", slippage="0.80", funding="0.20"))
    assert l.equity == D("1120.00")
    with pytest.raises(ValueError, match="trade net does not reconcile"):
        l.apply(trade("broken", "121.00", gross="121.00", fees="0.40", slippage="0.80", funding="0.20", minutes=2))
    assert l.equity == D("1120.00")
    with pytest.raises(ValueError, match="duplicate"):
        l.apply(AllocationEvent("event:different", "source:audited", T0 + timedelta(minutes=3), "USD", "adjustment", "0.20", reason="funding"))


def test_standalone_funding_adjustment_is_signed_realized_pnl_not_profit_allocation():
    l = ledger()
    l.apply(event("fee", "adjustment", "-1.00", reason="fee"))
    l.apply(event("funding", "adjustment", "0.20", reason="funding", minutes=2))
    d = l.apply(close("c1", START))
    assert d.net_pnl == D("-0.80")
    assert d.retained_profit == D("0") and d.reinvested_profit == D("0")
    assert l.reserve == D("0") and l.deployable == D("999.20")


def test_cross_midnight_mark_release_does_not_double_count_open_profit():
    l = ledger()
    l.apply(event("mark", "mark", "10.00"))
    first = l.apply(close("d1", START, mark="10.00"))
    assert first.net_pnl == D("10.00") and first.realized_net_pnl == D("0")
    assert l.deployable == D("1000.00") and l.equity == D("1010.00")
    l.apply(trade("exit", "8.00", released_mark="10.00", minutes=24 * 60 + 1))
    second = l.apply(close("d2", START + timedelta(days=1)))
    assert second.realized_net_pnl == D("8.00")
    assert second.unrealized_change == D("-10.00")
    assert second.net_pnl == D("-2.00")
    assert l.equity == D("1008.00") and l.reserve == D("4.00")


def test_negative_mark_release_and_partial_recovery():
    l = ledger()
    l.apply(event("mark", "mark", "-15.00"))
    l.apply(close("day1", START, mark="-15.00"))
    l.apply(trade("settled", "-12.00", released_mark="-15.00", minutes=1441))
    next_day = l.apply(close("day2", START + timedelta(days=1)))
    assert next_day.net_pnl == D("3.00")
    assert l.equity == D("988.00")


def test_duplicate_source_id_rejected_even_with_new_event_id():
    l = ledger()
    l.apply(trade("a", "1.00"))
    other = event("b", "adjustment", "-1.00", minutes=2, reason="fee")
    other = AllocationEvent(other.event_id, "source:a", other.timestamp, other.currency, other.kind, other.amount, reason="fee")
    with pytest.raises(ValueError, match="duplicate"):
        l.apply(other)
    assert l.equity == D("1001.00")


def test_reversed_time_and_missing_daily_close_fail_before_new_events():
    l = ledger()
    l.apply(trade("a", "5.00", minutes=5))
    with pytest.raises(ValueError, match="out-of-order"):
        l.apply(event("b", "mark", "0.00", minutes=1))
    with pytest.raises(ValueError, match="missing UTC day-close"):
        l.apply(trade("newday", "2.00", minutes=1441))
    with pytest.raises(ValueError, match="incomplete daily valuation"):
        l.report(START)
    l.apply(close("d1", START))
    l.apply(close("d2", START + timedelta(days=1)))
    assert l.report(START + timedelta(days=1))["flat_days"] == 1


def test_invalid_close_timestamp_rejected():
    l = ledger()
    with pytest.raises(ValueError, match="UTC day close"):
        l.apply(event("c", "day_close", "0.00", minutes=1439))
    assert l.daily == ()


def test_withdrawals_do_not_automatically_raid_retained_profit():
    l = ledger(reinvest_fraction="0")
    l.apply(trade("gain", "120.00"))
    assert l.reserve == D("120.00") and l.deployable == D("1000.00")
    with pytest.raises(ValueError, match="withdrawal"):
        l.apply(event("withdraw_all", "external_flow", "-1001.00", minutes=2))
    l.apply(event("withdraw", "external_flow", "-200.00", minutes=2))
    day = l.apply(close("d", START))
    assert day.net_pnl == D("120.00")
    assert day.external_flows == D("-200.00")
    assert l.equity == D("920.00")
    assert l.reserve == D("120.00")


def test_explicit_reserve_release_is_a_capital_transfer_not_pnl():
    l = ledger(reinvest_fraction="0")
    l.apply(trade("w", "120.00"))
    l.apply(event("transfer", "reserve_transfer", "40.00", minutes=2))
    d = l.apply(close("day", START))
    assert d.net_pnl == D("120.00") and d.retained_profit == D("120.00")
    assert l.deployable == D("1040.00") and l.reserve == D("80.00")
    with pytest.raises(ValueError, match="exceeds available"):
        l.apply(event("bad_transfer", "reserve_transfer", "100.00", minutes=1441))


def test_currency_mismatch_and_imprecise_money_fail_without_mutation():
    l = ledger()
    original = event("bad", "trade", "0.001", gross="0.001", fees="0.00", slippage="0.00", funding="0.00", released_mark="0.00")
    with pytest.raises(ValueError, match="quantized"):
        l.apply(original)
    bad_currency = AllocationEvent("event:eur", "source:eur", T0 + timedelta(minutes=2), "EUR", "mark", "2.00")
    with pytest.raises(ValueError, match="currency mismatch"):
        l.apply(bad_currency)
    assert l.equity == D("1000.00")
    assert l.deployable == D("1000.00")


def test_unqualified_event_types_metadata_and_float_amounts_rejected():
    l = ledger()
    with pytest.raises(ValueError):
        l.apply(event("unknown", "magical_pnl", "100.00"))
    with pytest.raises(ValueError):
        l.apply(event("floating", "adjustment", 1.0, reason="fee"))
    with pytest.raises(ValueError, match="reason"):
        l.apply(event("unattributed", "adjustment", "1.00"))
    with pytest.raises(ValueError, match="only trade"):
        l.apply(event("badmark", "mark", "10.00", gross="10.00"))
    assert l.equity == D("1000.00")


def test_risk_capacity_accounts_for_unrealized_loss_and_ignores_reserve():
    l = ledger(max_deployable="5000.00")
    l.apply(trade("win", "200.00"))
    assert l.risk_capacity == D("1100.00")
    l.apply(event("mark", "mark", "-1100.00", minutes=2))
    assert l.equity == D("100.00")
    assert l.risk_capacity == D("100.00")
    l.apply(event("deeper", "mark", "-1300.00", minutes=3))
    assert l.equity == D("-100.00")
    assert l.risk_capacity == D("0")


def test_report_refuses_later_unclosed_trades():
    l = ledger()
    l.apply(close("first", START))
    l.apply(trade("future", "5.00", minutes=1441))
    with pytest.raises(ValueError, match="incomplete daily valuation"):
        l.report(START)
    l.apply(close("next", START + timedelta(days=1)))
    assert l.report(START + timedelta(days=1))["net_pnl"] == "5.00"
