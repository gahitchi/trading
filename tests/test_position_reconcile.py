"""Causal position/fill bridge: partial fills, reversals, mark releases and journal restart."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from lab.position_reconcile import (
    CashAdjustment,
    PositionMark,
    SimulatedFill,
    compile_source,
    reconcile_to_journal,
)
from lab.profit_allocation import AllocationPolicy
from lab.profit_journal import ProfitJournal

START = date(2026, 11, 5)
T = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
D = Decimal


def policy():
    return AllocationPolicy(
        currency="USD", initial_capital="1000.00",
        quantum="0.01", reinvest_fraction="0.5",
        max_deployable="1500.00",
    )


def fill(identifier, minute, symbol, side, qty, price, marks, fee="0.00", slip="0.00"):
    return SimulatedFill(
        identifier, T + timedelta(minutes=minute), symbol, side,
        qty, price, fee, slip, marks,
    )


def mark(identifier, minute, marks, close=False):
    return PositionMark(identifier, T + timedelta(minutes=minute), marks, close)


def journal(tmp_path):
    return ProfitJournal(tmp_path / "ledger.jsonl", policy(), START)


def test_partial_long_fills_and_fee_reconciliation(tmp_path):
    data = [
        fill("open1", 1, "BTCUSDT", "BUY", "0.5", "100.00", {"BTCUSDT": "100.00"}, fee="0.10"),
        fill("open2", 2, "BTCUSDT", "BUY", "0.5", "104.00", {"BTCUSDT": "104.00"}, fee="0.10"),
        fill("close1", 3, "BTCUSDT", "SELL", "0.25", "108.00", {"BTCUSDT": "108.00"}, fee="0.10"),
        mark("day1", 1440, {"BTCUSDT": "108.00"}, close=True),
    ]
    j = journal(tmp_path)
    positions = reconcile_to_journal(j, data)
    assert positions["BTCUSDT"].quantity == D("0.75")
    assert positions["BTCUSDT"].average_price == D("102")
    # 0.25*(108-102) = +1.50 gross, 0.30 total fees, open PnL=0.75*6=4.50
    assert j.report(START)["net_pnl"] == "5.70"
    assert j.replay().reserve == D("0.70")
    assert j.replay().deployable == D("1000.50")


def test_short_reversal_and_loss_through_midnight(tmp_path):
    data = [
        fill("short", 1, "ETHUSDT", "SELL", "2", "100", {"ETHUSDT": "100"}),
        mark("day1", 1440, {"ETHUSDT": "90"}, close=True),
        fill("reversal", 1441, "ETHUSDT", "BUY", "3", "95", {"ETHUSDT": "95"}, fee="0.20"),
        mark("day2", 2880, {"ETHUSDT": "90"}, close=True),
    ]
    j = journal(tmp_path)
    positions = reconcile_to_journal(j, data)
    assert positions["ETHUSDT"].quantity == D("1")
    assert positions["ETHUSDT"].average_price == D("95")
    assert [str(x.net_pnl) for x in j.replay().daily] == ["20.00", "-15.20"]
    assert j.report(START + timedelta(days=1))["net_pnl"] == "4.80"


def test_opposing_unrealized_marks_across_two_positions_can_close_without_old_release_guard(tmp_path):
    data = [
        fill("long", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}),
        fill("short", 2, "ETHUSDT", "SELL", "1", "100", {"BTCUSDT": "110", "ETHUSDT": "100"}),
        mark("both", 3, {"BTCUSDT": "110", "ETHUSDT": "105"}),
        fill("close-long", 4, "BTCUSDT", "SELL", "1", "112", {"ETHUSDT": "105"}),
        mark("day", 1440, {"ETHUSDT": "105"}, close=True),
    ]
    j = journal(tmp_path)
    positions = reconcile_to_journal(j, data)
    assert set(positions) == {"ETHUSDT"}
    # BTC realized +12, ETH open -5. No rejected release of BTC +10 against aggregate +5.
    assert j.report(START)["net_pnl"] == "7.00"
    assert j.replay().reserve == D("6.00")


def test_fee_only_opening_fill_never_creates_positive_profit(tmp_path):
    data = [
        fill("open", 1, "SOLUSDT", "SELL", "2", "100", {"SOLUSDT": "100"}, fee="0.20"),
        mark("day", 1440, {"SOLUSDT": "100"}, close=True),
    ]
    j = journal(tmp_path)
    reconcile_to_journal(j, data)
    assert j.report(START)["net_pnl"] == "-0.20"
    assert j.replay().reserve == D("0")
    assert j.replay().deployable == D("999.80")


def test_no_double_count_slippage_and_standalone_funding(tmp_path):
    data = [
        fill("open", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}, fee="0.10", slip="0.10"),
        CashAdjustment("fund", T + timedelta(minutes=2), "-0.30", "funding"),
        fill("exit", 3, "BTCUSDT", "SELL", "1", "102", {}, fee="0.10", slip="0.10"),
        mark("day", 1440, {}, close=True),
    ]
    j = journal(tmp_path)
    reconcile_to_journal(j, data)
    assert j.report(START)["net_pnl"] == "1.30"
    assert j.replay().reserve == D("0.90")


def test_replay_restarts_without_duplicate_fills_and_append_only_updates(tmp_path):
    first = [
        fill("first", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}),
        mark("mark1", 2, {"BTCUSDT": "102"}),
    ]
    j = journal(tmp_path)
    reconcile_to_journal(j, first)
    assert len(j.events()) == 3
    j = journal(tmp_path)
    reconcile_to_journal(j, first)
    assert len(j.events()) == 3
    next_source = first + [
        fill("exit", 3, "BTCUSDT", "SELL", "1", "105", {}),
        mark("day", 1440, {}, close=True),
    ]
    positions = reconcile_to_journal(j, next_source)
    assert positions == {}
    assert len(j.events()) == 6
    assert j.report(START)["net_pnl"] == "5.00"


def test_changed_historical_fill_is_refused_without_mutating_journal(tmp_path):
    j = journal(tmp_path)
    original = [fill("a", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"})]
    reconcile_to_journal(j, original)
    before = j.path.read_bytes()
    with pytest.raises(ValueError, match="source/journal"):
        reconcile_to_journal(j, [fill("a", 1, "BTCUSDT", "BUY", "1", "99", {"BTCUSDT": "99"})])
    assert before == j.path.read_bytes()
    with pytest.raises(ValueError, match="outside supplied source"):
        reconcile_to_journal(j, [])


def test_duplicate_source_identifiers_rejected_before_publication(tmp_path):
    j = journal(tmp_path)
    data = [fill("repeat", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}),
            fill("repeat", 2, "BTCUSDT", "BUY", "1", "101", {"BTCUSDT": "101"})]
    with pytest.raises(ValueError, match="duplicate"):
        reconcile_to_journal(j, data)
    assert j.events() == ()


def test_missing_other_instrument_mark_rejected_and_not_written(tmp_path):
    j = journal(tmp_path)
    data = [
        fill("a", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}),
        fill("b", 2, "ETHUSDT", "BUY", "1", "100", {"ETHUSDT": "100"}),
    ]
    with pytest.raises(ValueError, match="cover exactly"):
        reconcile_to_journal(j, data)
    assert j.events() == ()


def test_no_lookahead_day_close_or_non_utc_fill(tmp_path):
    bad_timestamp = T + timedelta(days=1, minutes=1)
    data = [fill("first", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}),
            SimulatedFill("no-day-close", bad_timestamp, "BTCUSDT", "SELL", "1", "99", "0", "0", {})]
    with pytest.raises(ValueError, match="missing UTC day-close"):
        compile_source(policy(), START, data)
    with pytest.raises(ValueError, match="UTC"):
        compile_source(policy(), START, [SimulatedFill(
            "naive", datetime(2026, 11, 5, 0, 1), "BTCUSDT",
            "BUY", "1", "100", "0", "0", {"BTCUSDT": "100"},
        )])


def test_missing_marks_are_not_assumed_zero(tmp_path):
    j = journal(tmp_path)
    with pytest.raises(ValueError, match="cover exactly"):
        reconcile_to_journal(j, [fill("x", 1, "BTCUSDT", "BUY", "1", "100", {})])
    assert j.events() == ()


def test_short_to_flat_realization_and_zero_position_mark(tmp_path):
    source = [fill("open", 1, "BTCUSDT", "SELL", "2", "100", {"BTCUSDT": "100"}),
              fill("close", 2, "BTCUSDT", "BUY", "2", "96", {}),
              mark("day", 1440, {}, close=True)]
    j = journal(tmp_path)
    assert reconcile_to_journal(j, source) == {}
    assert j.report(START)["net_pnl"] == "8.00"


def test_unrealized_to_realized_revaluation_is_not_double_booked(tmp_path):
    source = [fill("long", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}),
              mark("price", 2, {"BTCUSDT": "110"}),
              fill("closing", 3, "BTCUSDT", "SELL", "1", "105", {}),
              mark("day", 1440, {}, close=True)]
    j = journal(tmp_path)
    reconcile_to_journal(j, source)
    assert j.report(START)["net_pnl"] == "5.00"
    assert j.replay().reserve == D("2.50")


@pytest.mark.parametrize("price,qty,fee", [
    ("NaN", "1", "0.01"), ("100", "0", "0.01"), ("100", "-2", "0.01"),
    ("100", "1", "-0.01"), ("Infinity", "1", "0.00"),
])
def test_nonfinite_or_invalid_fill_rejected(price, qty, fee):
    with pytest.raises(ValueError):
        compile_source(policy(), START, [fill("invalid", 1, "BTCUSDT", "BUY", qty, price,
                                                 {"BTCUSDT": "100"}, fee=fee)])


def test_open_position_cost_and_marked_loss_impact_total_equity(tmp_path):
    data = [
        fill("open", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "95"}, fee="0.20"),
        mark("day", 1440, {"BTCUSDT": "90"}, close=True),
    ]
    j = journal(tmp_path)
    reconcile_to_journal(j, data)
    assert j.report(START)["net_pnl"] == "-10.20"
    assert j.replay().risk_capacity == D("989.80")


def test_reconciliation_exact_trade_economics_after_multiple_partial_closures(tmp_path):
    data = [
        fill("open", 1, "BTCUSDT", "BUY", "2", "100", {"BTCUSDT": "100"}),
        fill("partial", 2, "BTCUSDT", "SELL", "0.5", "102", {"BTCUSDT": "102"}),
        fill("final", 3, "BTCUSDT", "SELL", "1.5", "104", {}),
        mark("day", 1440, {}, close=True),
    ]
    j = journal(tmp_path)
    reconcile_to_journal(j, data)
    assert j.report(START)["net_pnl"] == "7.00"
    assert j.report(START)["reserve"] == "3.50"
