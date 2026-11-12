"""Deterministic tests. Synthetic candles, no historical fit or holdout use."""

from dataclasses import replace

import pytest

from lab.bearish_hybrid import (
    HOUR_NS,
    MINUTE_NS,
    MarketEligibility,
    Bar,
    BearishHybrid,
    Costs,
    FundingEvent,
    OIObservation,
    Risk,
    Settings,
    VirtualAccount,
    paper_size,
    trailing_funding_apr,
)

T0 = 1_700_000_040_000_000_000  # aligned UTC minute end
COSTS = Costs(4, 3, 2, 1)
PAPER = VirtualAccount(10_000, 10_000, 0, 0, 5_000, 0, "run", True)
ZERO = VirtualAccount()


def baseline(n=95):
    return [Bar(T0 + i * MINUTE_NS, 100, 100.1, 99.9, 100, 100, 50) for i in range(n)]


def crash_sequence():
    rows = baseline()
    t = rows[-1].end_ns
    rows += [
        Bar(t + MINUTE_NS, 100, 100.1, 97.9, 98, 3_000, 100),
        Bar(t + 2 * MINUTE_NS, 98, 98.1, 97.98, 98.05, 100, 50),
        Bar(t + 3 * MINUTE_NS, 98.05, 98.09, 97.79, 97.8, 200, 30),
    ]
    return rows


def events(end_ns, *, rate=0.0001):
    return tuple(FundingEvent(end_ns - HOUR_NS - k * 8 * HOUR_NS, end_ns - HOUR_NS - k * 8 * HOUR_NS, rate) for k in range(8, -1, -1))


def market_at(end_ns, *, turnover=25_000_000, spot=True, perp=True, age_min=0):
    return MarketEligibility(end_ns - age_min * MINUTE_NS, spot, perp, turnover)


def oi_at(end_ns, *, rise=1.03, age_min=2):
    return OIObservation(end_ns - (age_min + 15) * MINUTE_NS, 1_000_000, end_ns - age_min * MINUTE_NS, 1_000_000 * rise)


def run(rows=None, *, account=PAPER, cost=COSTS, oi=None, market=None, funding=None, funding_verified=True, engine=None):
    rows = crash_sequence() if rows is None else rows
    engine = engine or BearishHybrid()
    out = []
    for b in rows:
        out.append(engine.on_bar(
            b, oi=oi if oi is not None else oi_at(b.end_ns),
            market=market if market is not None else market_at(b.end_ns),
            funding=funding if funding is not None else events(rows[0].end_ns),
            funding_verified=funding_verified, costs=cost, account=account,
        ))
    return out


def test_an_impulse_pause_and_new_break_generates_only_research_short():
    rows = crash_sequence()
    result = run(rows)
    assert result[-3].action == "WATCH" and result[-3].reasons == ("impulse_armed",)
    assert result[-2].action == "WATCH"
    last = result[-1]
    assert last.action == "RESEARCH_SHORT" and last.oi_regime == "build_up"
    assert last.priority == 2
    assert last.stop_price > last.reference_entry > last.target_price
    assert last.virtual_notional > 0
    assert last.actual_notional == 0
    assert all(x.action != "LONG" for x in result)


def test_no_drop_no_trade():
    assert not any(d.action == "RESEARCH_SHORT" for d in run(baseline(100)))


def test_no_rebound_does_not_chase_impulse():
    rows = crash_sequence()
    t = rows[-3].end_ns
    rows[-2] = Bar(t + MINUTE_NS, 97.91, 97.94, 97.89, 97.90, 100, 20)
    rows[-1] = Bar(t + 2 * MINUTE_NS, 97.90, 97.93, 97.7, 97.75, 200, 20)
    assert run(rows)[-1].action != "RESEARCH_SHORT"


def test_zero_capital_blocks_virtual_trade_without_disabling_observation():
    result = run(account=ZERO)
    assert result[-3].action == "WATCH"
    assert result[-1].action == "UNFUNDED_SIGNAL"
    assert result[-1].virtual_notional == result[-1].actual_notional == 0


@pytest.mark.parametrize("a", [replace(PAPER, kill_mode="halt"), replace(PAPER, kill_mode="flatten"), replace(PAPER, risk_ready=False), replace(PAPER, day_pnl=-150), replace(PAPER, open_positions=1)])
def test_guarded_virtual_account_never_suggests_size(a):
    assert run(account=a)[-1].action == "FLAT"


def test_conservative_cap_on_size_not_position_profit_claim():
    result = run()[-1]
    assert result.virtual_notional <= 3_000
    assert result.virtual_notional * ((result.stop_price / result.reference_entry - 1) + COSTS.round_trip_bps / 10_000) <= 25 + 1e-6
    assert paper_size(PAPER, Risk(), 40, 17) <= 3_000


def test_stale_and_missing_oi_fail_closed():
    rows = crash_sequence()
    stale = oi_at(rows[-1].end_ns, age_min=16)
    assert "oi_stale_or_invalid" in run(oi=stale)[-1].reasons
    eng = BearishHybrid()
    result = []
    for b in rows:
        result.append(eng.on_bar(b, oi=None, market=market_at(b.end_ns), funding=events(rows[0].end_ns), funding_verified=True, costs=COSTS, account=PAPER))
    assert "oi_missing" in result[-1].reasons


def test_flush_is_short_candidate_not_wick_long():
    rows = crash_sequence()
    last = run(oi=oi_at(rows[-1].end_ns, rise=0.97))[-1]
    assert last.action == "RESEARCH_SHORT" and last.oi_regime == "liquidation_flush"
    assert last.priority == 1


def test_untrusted_funding_blocks_and_funding_positive_only_prioritizes():
    rows = crash_sequence()
    assert "funding_not_verified" in run(funding_verified=False)[-1].reasons
    negative = events(rows[0].end_ns, rate=-0.0001)
    last = run(funding=negative)[-1]
    assert last.action == "RESEARCH_SHORT" and last.priority == 1
    assert last.funding_apr_72h < 0
    eng = BearishHybrid()
    got = [eng.on_bar(b, oi=oi_at(b.end_ns), market=market_at(b.end_ns), funding=(), funding_verified=True, costs=COSTS, account=PAPER) for b in rows]
    assert "funding_history_unavailable" in got[-1].reasons


def test_cost_gate_stops_pretend_profit():
    last = run(cost=Costs(15, 15, 5, 5))[-1]
    assert "friction_too_high" in last.reasons


def test_gap_resets_state_no_stale_confirmation():
    rows = crash_sequence()
    rows[-2] = replace(rows[-2], end_ns=rows[-2].end_ns + MINUTE_NS)
    rows[-1] = replace(rows[-1], end_ns=rows[-1].end_ns + MINUTE_NS)
    result = run(rows)
    assert result[-2].reasons == ("history_gap",)
    assert result[-1].action != "RESEARCH_SHORT"


def test_duplicate_timestamp_fails_explicitly():
    rows = baseline()
    e = BearishHybrid()
    for b in rows:
        e.on_bar(b, oi=None, market=market_at(b.end_ns), funding=(), funding_verified=False, costs=COSTS, account=PAPER)
    with pytest.raises(ValueError, match="out-of-order"):
        e.on_bar(rows[-1], oi=None, market=market_at(rows[-1].end_ns), funding=(), funding_verified=False, costs=COSTS, account=PAPER)


def test_funding_uses_only_known_publications_and_variable_intervals():
    now = T0 + 100 * HOUR_NS
    known = (
        FundingEvent(now - 8 * HOUR_NS, now - 8 * HOUR_NS, 0.0001),
        FundingEvent(now - 4 * HOUR_NS, now - 3 * HOUR_NS, 0.0002),
        FundingEvent(now, now + MINUTE_NS, 100.0),  # invisible future publication
    )
    apr, count = trailing_funding_apr(known, now)
    assert count == 2 and apr == pytest.approx(0.0003 * 365 * 24 / 72 * 100)
    with pytest.raises(ValueError):
        trailing_funding_apr(tuple(reversed(known)), now)


def test_lookahead_funding_does_not_change_decision():
    rows = crash_sequence()
    known = events(rows[0].end_ns)
    future = FundingEvent(rows[-1].end_ns, rows[-1].end_ns + MINUTE_NS, 100)
    a, b = run(funding=known)[-1], run(funding=known + (future,))[-1]
    assert a == b


def test_short_needs_complete_causal_confirmation_and_oi_is_not_proof_of_bears():
    rows = crash_sequence()
    unconfirmed = rows[:-1]
    assert all(x.action != "RESEARCH_SHORT" for x in run(unconfirmed))
    assert run(rows)[-1].oi_regime == "build_up"
    assert run(rows)[-1].reasons == ("impulse_pause_breakdown",)


def test_invalid_ohlc_and_risk_data_are_rejected():
    rows = crash_sequence()
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        run(rows[:-1] + [replace(rows[-1], low=105)])
    with pytest.raises(ValueError, match="invalid virtual account"):
        run(account=replace(PAPER, current_gross_notional=-2))


def test_future_open_interest_is_rejected_for_trade():
    rows = crash_sequence()
    t = rows[-1].end_ns
    future = OIObservation(t - 16 * MINUTE_NS, 100, t + MINUTE_NS, 103)
    assert "oi_stale_or_invalid" in run(oi=future)[-1].reasons


def test_market_listings_liquidity_and_freshness_are_required():
    rows = crash_sequence()
    t = rows[-1].end_ns
    for bad in (market_at(t, turnover=1_000_000), market_at(t, spot=False), market_at(t, perp=False), market_at(t, age_min=2)):
        assert "market_ineligible" in run(market=bad)[-1].reasons
    eng = BearishHybrid()
    result = [eng.on_bar(b, oi=oi_at(b.end_ns), market=None, funding=events(rows[0].end_ns), funding_verified=True, costs=COSTS, account=PAPER) for b in rows]
    assert "market_eligibility_missing" in result[-1].reasons


def test_single_confirmation_candle_cannot_simultaneously_create_its_own_pullback():
    rows = crash_sequence()
    t = rows[-3].end_ns
    # The first recovery bar never rises 0.25 ATR above the impulse low.
    rows[-2] = Bar(t + MINUTE_NS, 97.91, 97.92, 97.89, 97.91, 100, 50)
    # This candle has a qualifying high AND closes below the low. Within
    # minute OHLC we cannot know whether that high preceded the low.
    rows[-1] = Bar(t + 2 * MINUTE_NS, 97.91, 98.09, 97.79, 97.80, 200, 30)
    assert run(rows)[-1].action != "RESEARCH_SHORT"