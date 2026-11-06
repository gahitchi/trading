"""Portable daily scorecard acceptance: all days, recovery, cost and capital."""

from datetime import datetime, timedelta

import pytest

from lab.daily_scorecard import daily_scorecard
from lab.profit_journal import ProfitJournal
from tests.test_profit_allocation import START, close, event, policy, trade


def opened(tmp_path, **policy_kwargs):
    return ProfitJournal(tmp_path / "scorecard.jsonl", policy(**policy_kwargs), START)


def close_day(journal, i, days_from_start, mark="0.00"):
    return journal.append(close(i, START + timedelta(days=days_from_start), mark))


def test_variable_daily_pnl_counts_flat_and_zero_trade_days(tmp_path):
    j = opened(tmp_path)
    j.append(trade("small", "0.20"))
    close_day(j, "day1", 0)
    j.append(trade("large", "120.00", minutes=1441))
    close_day(j, "day2", 1)
    j.append(trade("loss", "-80.00", minutes=2881))
    close_day(j, "day3", 2)
    close_day(j, "day4", 3)
    result = daily_scorecard(j, START + timedelta(days=3))
    assert result["status"] == "research_accounting_scorecard_unqualified"
    assert (result["winning_days"], result["losing_days"], result["flat_days"]) == (2, 1, 1)
    assert (result["active_trade_days"], result["zero_trade_days"], result["trade_events"]) == (3, 1, 3)
    assert result["mean_daily_net_pnl"] == "10.05"
    assert result["median_daily_net_pnl"] == "0.10"
    assert result["p05_daily_net_pnl_nearest_rank"] == "-80.00"
    assert result["total_net_pnl"] == "40.20"
    assert result["actual_end_equity"] == "1040.20"
    assert result["flow_neutral_end_equity"] == "1040.20"
    assert result["flow_neutral_max_drawdown"] == "80.00"
    assert result["max_consecutive_zero_trade_days"] == 1
    assert result["max_underwater_calendar_days"] == 2
    assert result["current_unrecovered_calendar_days"] == 2
    assert result["max_completed_recovery_calendar_days"] is None
    assert [x["net_pnl"] for x in result["daily"]] == ["0.20", "120.00", "-80.00", "0.00"]
    assert [x["trade_event_count"] for x in result["daily"]] == [1, 1, 1, 0]
    assert result["daily"][-1]["zero_trade_day"] is True
    assert result["daily"][-1]["daily_return_on_start_equity_pct"] == "0"
    for name in ("daily_edge_qualified", "source_authenticated", "risk_ready", "economic_posting_allowed"):
        assert result[name] is False


def test_recovering_previous_high_reports_actual_completed_duration(tmp_path):
    j = opened(tmp_path)
    j.append(trade("win", "50.00"))
    close_day(j, "d1", 0)
    j.append(trade("lose", "-20.00", minutes=1441))
    close_day(j, "d2", 1)
    close_day(j, "d3", 2)
    j.append(trade("recover", "20.00", minutes=4321))
    close_day(j, "d4", 3)
    report = daily_scorecard(j, START + timedelta(days=3))
    assert report["max_underwater_calendar_days"] == 2
    assert report["max_completed_recovery_calendar_days"] == 2
    assert report["current_unrecovered_calendar_days"] == 0
    assert report["flow_neutral_max_drawdown"] == "20.00"


def test_external_deposit_does_not_fabricate_profit_or_eliminate_drawdown(tmp_path):
    j = opened(tmp_path)
    close_day(j, "d1", 0)
    j.append(event("deposit", "external_flow", "500.00", minutes=1441))
    close_day(j, "d2", 1)
    j.append(trade("loss", "-10.00", minutes=2881))
    close_day(j, "d3", 2)
    r = daily_scorecard(j, START + timedelta(days=2))
    assert r["cumulative_external_flows"] == "500.00"
    assert r["total_net_pnl"] == "-10.00"
    assert r["actual_end_equity"] == "1490.00"
    assert r["flow_neutral_end_equity"] == "990.00"
    assert r["flow_neutral_max_drawdown"] == "10.00"
    assert r["daily"][1]["net_pnl"] == "0.00"
    assert r["daily"][1]["zero_trade_day"] is True
    assert r["daily"][1]["external_flows"] == "500.00"


def test_fees_slippage_and_funding_are_descriptive_not_double_counted(tmp_path):
    j = opened(tmp_path)
    j.append(trade(
        "filled", "10.00", gross="11.30", fees="1.00",
        slippage="0.50", funding="0.20",
    ))
    j.append(event("fee", "adjustment", "-0.05", minutes=2, reason="fee"))
    j.append(event("funding", "adjustment", "-0.03", minutes=3, reason="funding"))
    close_day(j, "d1", 0)
    r = daily_scorecard(j, START)
    assert r["total_net_pnl"] == "9.92"
    assert r["total_recorded_trade_fees"] == "1.00"
    assert r["total_recorded_additional_slippage"] == "0.50"
    assert r["trade_funding_already_included_in_trade_net"] == "0.20"
    assert r["standalone_funding_adjustments"] == "-0.03"
    assert r["daily"][0]["cash_adjustments_including_funding"] == "-0.08"
    assert r["daily"][0]["reinvested_realized_profit"] == "5.00"
    assert r["daily"][0]["retained_realized_profit"] == "5.00"


def test_mark_to_market_and_cross_midnight_release_do_not_double_count(tmp_path):
    j = opened(tmp_path)
    j.append(event("marked", "mark", "10.00"))
    close_day(j, "day1", 0, "10.00")
    j.append(trade("release", "8.00", released_mark="10.00", minutes=1441))
    close_day(j, "day2", 1)
    r = daily_scorecard(j, START + timedelta(days=1))
    assert [x["net_pnl"] for x in r["daily"]] == ["10.00", "-2.00"]
    assert r["daily"][0]["realized_net_pnl"] == "0"
    assert r["daily"][1]["unrealized_change"] == "-10.00"
    assert r["total_net_pnl"] == "8.00"
    assert r["active_trade_days"] == 1
    assert r["zero_trade_days"] == 1


def test_reinvestment_changes_buckets_not_equity_or_profit(tmp_path):
    comparisons = []
    for name, fraction in (("zero", "0.0"), ("half", "0.5"), ("all", "1.0")):
        j = ProfitJournal(tmp_path / f"{name}.jsonl", policy(reinvest_fraction=fraction), START)
        j.append(trade("winner", "20.00"))
        close_day(j, "day1", 0)
        comparisons.append(daily_scorecard(j, START))
    assert {r["total_net_pnl"] for r in comparisons} == {"20.00"}
    assert {r["actual_end_equity"] for r in comparisons} == {"1020.00"}
    assert [r["final_deployable"] for r in comparisons] == ["1000.00", "1010.00", "1020.00"]
    assert [r["final_reserve"] for r in comparisons] == ["20.00", "10.00", "0.00"]


def test_many_zero_trade_days_have_exact_zero_profit(tmp_path):
    j = opened(tmp_path)
    for n in range(7):
        close_day(j, f"day{n}", n)
    r = daily_scorecard(j, START + timedelta(days=6))
    assert r["total_days"] == 7
    assert r["flat_days"] == 7
    assert r["zero_trade_days"] == 7
    assert r["max_consecutive_zero_trade_days"] == 7
    assert r["trade_events"] == 0
    assert r["trades_per_active_day"] is None
    assert r["profit_factor"] is None
    assert r["mean_daily_net_pnl"] == "0.00"
    assert r["flow_neutral_max_drawdown"] == "0"


def test_reopened_journal_returns_same_scorecard_without_writes(tmp_path):
    j = opened(tmp_path)
    j.append(trade("one", "1.25"))
    close_day(j, "day1", 0)
    baseline = daily_scorecard(j, START)
    before = j.path.read_bytes()
    reopened = ProfitJournal(j.path, j.policy, START)
    assert daily_scorecard(reopened, START) == baseline
    assert j.path.read_bytes() == before


def test_missing_utc_day_close_blocks_reporting_instead_of_padding(tmp_path):
    j = opened(tmp_path)
    j.append(trade("first", "5.00"))
    with pytest.raises(ValueError, match="incomplete daily valuation coverage"):
        daily_scorecard(j, START)
    close_day(j, "day1", 0)
    with pytest.raises(ValueError, match="incomplete daily valuation coverage"):
        daily_scorecard(j, START + timedelta(days=1))
    with pytest.raises(ValueError, match="date"):
        daily_scorecard(j, datetime(2026, 11, 5))
    with pytest.raises(ValueError, match="date"):
        daily_scorecard(j, None)


def test_tampered_journal_blocks_scorecard(tmp_path):
    j = opened(tmp_path)
    j.append(trade("win", "1.00"))
    close_day(j, "day1", 0)
    original = j.path.read_bytes()
    j.path.write_bytes(original.replace(b"1.00", b"9.00", 1))
    with pytest.raises(ValueError, match=r"digest|mismatch"):
        daily_scorecard(j, START)


def test_missing_journal_checkpoint_blocks_scorecard(tmp_path):
    j = opened(tmp_path)
    close_day(j, "day1", 0)
    j._head.unlink()
    with pytest.raises(ValueError, match="checkpoint is missing"):
        daily_scorecard(j, START)
