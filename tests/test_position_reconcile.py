"""Causal position/fill bridge: partial fills, reversals, mark releases and journal restart."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from lab.position_reconcile import (
    CashAdjustment,
    MarkQuote,
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


def quotes(identifier, minute, prices):
    ts = T + timedelta(minutes=minute)
    return {
        sym: MarkQuote(px, ts, f"{identifier}:{sym}")
        for sym, px in prices.items()
    }


def fill(identifier, minute, symbol, side, qty, price, marks, fee="0.00", slip="0.00"):
    return SimulatedFill(
        identifier, T + timedelta(minutes=minute), symbol, side,
        qty, price, fee, slip, quotes(identifier, minute, marks),
    )


def mark(identifier, minute, marks, close=False):
    return PositionMark(
        identifier, T + timedelta(minutes=minute),
        quotes(identifier, minute, marks), close,
    )


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
    next_source = [
        *first,
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
            "BUY", "1", "100", "0", "0",
            {"BTCUSDT": MarkQuote("100", datetime(2026, 11, 5, 0, 1), "quote:naive")},
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


def test_atomic_batch_validation_rejects_entire_invalid_fill_revaluation(tmp_path):
    from lab.profit_allocation import AllocationEvent

    j = journal(tmp_path)
    initial = j.path.read_bytes()
    cash = AllocationEvent("fill.cash", "fill.cash", T + timedelta(minutes=1),
                           "USD", "trade", "-1.00", gross="0.00", fees="1.00",
                           slippage="0.00", funding="0.00", released_mark="0.00")
    invalid_mark = AllocationEvent("fill.mark", "fill.mark", T + timedelta(minutes=2),
                                   "USD", "mark", "bad")
    with pytest.raises(ValueError, match="invalid decimal"):
        j.append_batch([cash, invalid_mark])
    assert j.path.read_bytes() == initial
    assert j.events() == ()


def test_batch_checkpoint_failure_never_claims_successful_fill(tmp_path, monkeypatch):
    import lab.profit_journal as mod
    j = journal(tmp_path)
    original = mod.atomic_json

    def fail_checkpoint(*args, **kwargs):
        raise OSError("head disk failure")

    monkeypatch.setattr(mod, "atomic_json", fail_checkpoint)
    with pytest.raises(OSError, match="head disk failure"):
        reconcile_to_journal(
            j, [fill("one", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"})]
        )
    monkeypatch.setattr(mod, "atomic_json", original)
    with pytest.raises(ValueError, match="journal/checkpoint mismatch"):
        j.replay()


def test_future_mark_source_is_rejected_before_any_cashflow_is_booked(tmp_path):
    j = journal(tmp_path)
    observation = MarkQuote("110", T + timedelta(minutes=2), "source:future")
    row = SimulatedFill(
        "future", T + timedelta(minutes=1), "BTCUSDT", "BUY", "1", "100",
        "0.00", "0.00", {"BTCUSDT": observation},
    )
    with pytest.raises(ValueError, match="future-dated mark"):
        reconcile_to_journal(j, [row])
    assert j.events() == ()


def test_stale_mark_is_not_accepted_for_replay(tmp_path):
    j = journal(tmp_path)
    source = fill("first", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"})
    too_old = MarkQuote("101", T + timedelta(minutes=1), "source:old")
    row = PositionMark("stale", T + timedelta(minutes=2), {"BTCUSDT": too_old})
    with pytest.raises(ValueError, match="stale position mark"):
        reconcile_to_journal(j, [source, row])
    assert j.events() == ()


def test_exact_five_second_mark_age_is_eligible_without_lookahead(tmp_path):
    j = journal(tmp_path)
    source = fill("first", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"})
    close_ts = T + timedelta(days=1)
    snapshot = PositionMark(
        "midnight", close_ts,
        {"BTCUSDT": MarkQuote("105", close_ts - timedelta(seconds=5), "source:recent")},
        True,
    )
    reconcile_to_journal(j, [source, snapshot])
    assert j.report(START)["net_pnl"] == "5.00"


def test_unsourced_string_price_is_rejected_and_not_silently_assumed_fresh(tmp_path):
    j = journal(tmp_path)
    source = SimulatedFill(
        "first", T + timedelta(minutes=1),
        "BTCUSDT", "BUY", "1", "100", "0", "0",
        {"BTCUSDT": "100"},
    )
    with pytest.raises(ValueError, match="timestamped MarkQuote"):
        reconcile_to_journal(j, [source])
    assert j.events() == ()


def test_changed_quote_provenance_with_same_pnl_cannot_replay_as_old_source(tmp_path):
    j = journal(tmp_path)
    initial = fill("open", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"})
    reconcile_to_journal(j, [initial])
    before = j.path.read_bytes()
    same_economics_changed_quote = SimulatedFill(
        "open", initial.timestamp, initial.symbol, initial.side, initial.quantity,
        initial.fill_price, initial.fee, initial.additional_slippage,
        {"BTCUSDT": MarkQuote("100", initial.timestamp - timedelta(seconds=1), "new:source")},
    )
    with pytest.raises(ValueError, match="source/journal economic event mismatch"):
        reconcile_to_journal(j, [same_economics_changed_quote])
    assert j.path.read_bytes() == before


def test_changed_fee_source_with_same_net_value_fails_immutable_evidence_binding(tmp_path):
    j = journal(tmp_path)
    initial = fill("open", 1, "BTCUSDT", "BUY", "1", "100", {"BTCUSDT": "100"}, fee="0.10")
    reconcile_to_journal(j, [initial])
    before = j.path.read_bytes()
    # Fee changes while an additional slippage cost decreases equally:
    # net P&L is identical, but economic provenance must NOT match.
    modified = SimulatedFill(
        "open", initial.timestamp, "BTCUSDT", "BUY", "1", "100", "0.05", "0.05",
        initial.marks,
    )
    with pytest.raises(ValueError, match="source/journal economic event mismatch"):
        reconcile_to_journal(j, [modified])
    assert j.path.read_bytes() == before
