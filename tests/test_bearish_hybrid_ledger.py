"""Synthetic daily virtual ledger tests. No market fetch or real accounts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_replay import ReplayAssumptions, ShortTrade, _fill
from lab.bearish_hybrid import MINUTE_NS, Bar
from lab.bearish_hybrid_ledger import daily_hybrid_virtual_ledger
from lab.bearish_hybrid_replay import DAY_NS, HybridObservation, HybridReplay
from tests.test_bearish_hybrid import COSTS, PAPER, ZERO


def frame(*, days=2, start_end_ns=1_700_006_400_000_000_000):
    # UTC midnight anchor; every day contains exactly 1440 minute-end stamps.
    assert start_end_ns % DAY_NS == 0
    rows = []
    for i in range(1, 1_440 * days + 1):
        bar = Bar(start_end_ns + i * MINUTE_NS, 100, 101, 99, 100, 100, 50)
        rows.append(HybridObservation(bar, None, None, (), False, True))
    return rows, start_end_ns + (days + 1) * DAY_NS


def trade_at(rows, *, entry_i=42, exit_i=43, exit_ref=99.0, stop=101.5, target=98.0):
    # i denotes the full-day candle's zero-based index.
    start = rows[entry_i].bar.end_ns - MINUTE_NS
    end = rows[exit_i].bar.end_ns
    entry_ref = rows[entry_i].bar.open
    roundtrip = RoundTripCosts(
        COSTS.fee_per_side_bps, COSTS.slippage_per_side_bps, COSTS.full_spread_bps, COSTS.adverse_funding_bps
    )
    executed = _fill(entry_ref, exit_ref, roundtrip)
    reason = "time"
    return ShortTrade(
        signal_end_ns=start - 2 * MINUTE_NS,
        entry_minute_start_ns=start,
        exit_bar_end_ns=end,
        setup="impulse_pause_breakdown",
        exit_reason=reason,
        entry_reference=entry_ref,
        exit_reference=exit_ref,
        entry_fill=executed["entry_fill"],
        exit_fill=executed["exit_fill"],
        stop_reference=stop,
        target_reference=target,
        gross_return=executed["gross_return"],
        net_return=executed["net_return"],
        fee_cost=executed["fee_cost"],
        slippage_cost=executed["slippage_cost"],
        spread_cost=executed["spread_cost"],
        funding_reserve_cost=executed["funding_reserve_cost"],
    )


def result(trades=(), notionals=(), *, hold=2, halted=False, unresolved=0):
    return HybridReplay(
        tuple(trades), tuple(notionals), (), (), 0, unresolved, halted, ReplayAssumptions(1, hold)
    )


def mark(rows, bound, replay, *, account=PAPER):
    return daily_hybrid_virtual_ledger(rows, replay, costs=COSTS, account=account, boundary_ns=bound)


def test_flat_two_days_have_no_fictitious_profit():
    rows, bound = frame()
    ledger = mark(rows, bound, result())
    assert len(ledger.days) == ledger.flat_days == 2
    assert ledger.positive_days == ledger.negative_days == 0
    assert ledger.ending_equity == 10_000
    assert ledger.total_return == 0
    assert not ledger.funded_with_real_cash and not ledger.position_sizing_compounds


def test_realized_net_reference_pnl_and_next_flat_day():
    rows, bound = frame()
    t = trade_at(rows)
    rows[43] = replace(rows[43], bar=replace(rows[43].bar, close=99.0))
    ledger = mark(rows, bound, result((t,), (1_000,)))
    assert ledger.days[0].entries == ledger.days[0].exits == 1
    assert ledger.days[1].entries == ledger.days[1].exits == 0
    assert ledger.ending_equity == pytest.approx(10_000 + 1_000 * t.net_return)
    assert ledger.days[1].net_pnl == 0
    assert sum(d.net_pnl for d in ledger.days) == pytest.approx(ledger.ending_equity - 10_000)


def test_overnight_inventory_marks_costs_then_realizes_once():
    rows, bound = frame()
    t = trade_at(rows, entry_i=1439, exit_i=1441)
    rows[1441] = replace(rows[1441], bar=replace(rows[1441].bar, close=99.0))
    ledger = mark(rows, bound, result((t,), (1_000,), hold=3))
    first, second = ledger.days
    assert first.open_position and first.entries == 1 and first.exits == 0
    assert first.realized_cash == PAPER.equity and first.end_equity < PAPER.equity
    assert not second.open_position and second.entries == 0 and second.exits == 1
    assert second.realized_cash == pytest.approx(10_000 + 1_000 * t.net_return)
    assert first.net_pnl + second.net_pnl == pytest.approx(1_000 * t.net_return)


def test_unresolved_and_unfunded_accounts_do_not_report_equity():
    rows, bound = frame()
    with pytest.raises(ValueError, match="unresolved"):
        mark(rows, bound, result(halted=True, unresolved=1))
    with pytest.raises(ValueError, match="funded"):
        mark(rows, bound, result(), account=ZERO)


def test_gap_and_partial_day_rejected():
    rows, bound = frame()
    with pytest.raises(ValueError, match="complete UTC days"):
        mark(rows[:-1], bound, result())
    rows[100] = replace(rows[100], source_complete=False)
    with pytest.raises(ValueError, match="untrusted"):
        mark(rows, bound, result())


def test_protected_holdout_and_inconsistent_exit_refused():
    rows, bound = frame()
    with pytest.raises(ValueError, match="protected holdout"):
        mark(rows, rows[-1].bar.end_ns, result())
    t = trade_at(rows)
    rows[43] = replace(rows[43], bar=replace(rows[43].bar, close=99))
    with pytest.raises(ValueError, match="contradicts"):
        mark(rows, bound, result((replace(t, exit_reason="target"),), (1_000,)))


def test_notional_tampering_cannot_create_an_equity_claim():
    rows, bound = frame()
    t = trade_at(rows)
    rows[43] = replace(rows[43], bar=replace(rows[43].bar, close=99))
    with pytest.raises(ValueError, match="notional"):
        mark(rows, bound, result((t,), (-100,)))
    with pytest.raises(ValueError, match="accounting mismatch"):
        mark(rows, bound, result((t,), ()))


def test_fabricated_overlarge_notional_cannot_inflate_virtual_returns():
    rows, bound = frame()
    t = trade_at(rows)
    rows[43] = replace(rows[43], bar=replace(rows[43].bar, close=99))
    with pytest.raises(ValueError, match="risk cap"):
        mark(rows, bound, result((t,), (9_999,)))
