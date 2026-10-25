"""Default-universe equity scouting: no forced trades, point-in-time evidence only."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.discovery import DiscoveryMode, EquityObservation, InsiderFiling, ScoutPolicy, scout_equities

D = Decimal
DAY = 86_400_000_000_000
NOW = 400 * DAY


def market(ticker: str, **changes) -> EquityObservation:
    return replace(
        EquityObservation(
            ticker=ticker,
            venue="XNAS",
            currency="USD",
            received_ns=NOW - DAY,
            session_ns=NOW - DAY - 1_000_000_000,
            price=D("100"),
            median_dollar_volume=D("20000000"),
            spread_bps=D("8"),
            market_cap=D("10000000000"),
            history_sessions=400,
            market_source="tested_market_vendor_snapshot_sha256",
        ),
        **changes,
    )


def insider(ticker: str, accession="0000000001-26-000001", **changes) -> InsiderFiling:
    return replace(
        InsiderFiling(
            ticker=ticker,
            accession=accession,
            transaction_ns=NOW - 15 * DAY,
            filed_ns=NOW - 12 * DAY,
            received_ns=NOW - 11 * DAY,
            shares=D("1000"),
            price=D("97.50"),
            transaction_code="P",
            acquired_disposed="A",
            non_derivative=True,
            direct_ownership=True,
            plan_10b5_1=False,
        ),
        **changes,
    )


def test_blank_watchlist_triggers_automatic_research_ranking_but_never_orders():
    out = scout_equities((), (market("MSFT"), market("AAPL")), (insider("AAPL"),), decision_ns=NOW)
    assert out.mode is DiscoveryMode.SCOUT
    assert out.evaluated_universe == 2
    assert [x.ticker for x in out.candidates] == ["AAPL", "MSFT"]
    assert out.candidates[0].insider_purchase_count == 1
    assert out.candidates[0].insider_purchased_notional == D("97500")
    assert out.research_only and not out.orders_authorized
    assert all(not c.eligible_for_orders for c in out.candidates)


def test_blank_watchlist_without_data_means_no_trades_or_fake_discoveries():
    out = scout_equities((), (), (), decision_ns=NOW)
    assert out.mode is DiscoveryMode.SCOUT
    assert not out.candidates and not out.orders_authorized


def test_explicit_watchlist_never_expands_from_market_scout():
    out = scout_equities(("AAPL", "TSLA"), (market("MSFT"), market("AAPL")), (), decision_ns=NOW)
    assert out.mode is DiscoveryMode.EXPLICIT
    assert [x.ticker for x in out.candidates] == ["AAPL"]
    assert ("TSLA", "missing point-in-time market evidence") in out.skipped
    assert "MSFT" not in {x.ticker for x in out.candidates}


def test_future_market_snapshot_is_invisible_and_cannot_override_as_known_snapshot():
    old = market("AAPL")
    future = market("AAPL", received_ns=NOW + DAY, market_cap=D("1000000000000"))
    out = scout_equities((), (future, old), (), decision_ns=NOW)
    assert len(out.candidates) == 1
    assert out.candidates[0].observed_ns == old.received_ns
    assert scout_equities((), (future,), (), decision_ns=NOW).candidates == ()


def test_recent_receipt_of_ancient_prices_does_not_qualify_a_stock():
    old_session = market("AAPL", session_ns=NOW - 30 * DAY, received_ns=NOW - 1)
    out = scout_equities((), (old_session,), (), decision_ns=NOW)
    assert not out.candidates
    assert "stale" in out.skipped[0][1]


def test_conflicting_primary_listings_fail_closed_without_silent_venue_substitution():
    a = market("AAPL", venue="XNAS")
    b = market("AAPL", venue="XNYS")
    left = scout_equities((), (a, b), (), decision_ns=NOW)
    right = scout_equities((), (b, a), (), decision_ns=NOW)
    assert left == right
    assert not left.candidates
    assert "ambiguous" in left.skipped[0][1]


def test_insider_is_recognized_only_after_public_filing_reception():
    older = insider("AAPL", received_ns=NOW + 1, filed_ns=NOW - DAY)
    out = scout_equities((), (market("AAPL"),), (older,), decision_ns=NOW)
    assert out.candidates[0].insider_purchase_count == 0
    assert out.candidates[0].filing_accessions == ()


@pytest.mark.parametrize(
    "change",
    [
        {"transaction_code": "S"},
        {"acquired_disposed": "D"},
        {"non_derivative": False},
        {"direct_ownership": False},
        {"plan_10b5_1": True},
        {"plan_10b5_1": None},
        {"received_ns": NOW - 100 * DAY, "filed_ns": NOW - 102 * DAY, "transaction_ns": NOW - 103 * DAY},
    ],
)
def test_non_discretionary_or_stale_form4_cannot_boost_ranking(change):
    row = insider("AAPL", **change)
    out = scout_equities((), (market("AAPL"),), (row,), decision_ns=NOW)
    assert out.candidates[0].insider_purchase_count == 0


def test_repeated_accession_not_counted_as_two_independent_filings():
    a = insider("AAPL")
    b = insider("AAPL", price=D("100"))
    out = scout_equities((), (market("AAPL"),), (a, b), decision_ns=NOW)
    assert out.candidates[0].insider_purchase_count == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"point_in_time_membership": False}, "listing"),
        ({"received_ns": NOW - 8 * DAY, "session_ns": NOW - 8 * DAY - 1_000_000_000}, "stale"),
        ({"history_sessions": 10}, "historical"),
        ({"market_cap": D("100")}, "capitalization"),
        ({"median_dollar_volume": D("1000")}, "liquidity"),
        ({"spread_bps": D("100")}, "spread"),
    ],
)
def test_pretrade_universe_screen_rejects_low_quality_evidence(change, reason):
    out = scout_equities((), (market("AAPL", **change),), (), decision_ns=NOW)
    assert not out.candidates
    assert reason in out.skipped[0][1]


def test_multiple_purchase_lines_in_one_form4_sum_notional_but_count_only_one_filing():
    first = insider("AAPL", transaction_line="1", shares=D("1000"))
    second = insider("AAPL", transaction_line="2", shares=D("300"))
    repeated_first = insider("AAPL", transaction_line="1", shares=D("1000"))
    out = scout_equities((), (market("AAPL"),), (second, repeated_first, first), decision_ns=NOW)
    assert out.candidates[0].insider_purchase_count == 1
    assert out.candidates[0].filing_accessions == (first.accession,)
    assert out.candidates[0].insider_purchased_notional == D("126750")


def test_amended_filing_is_not_double_counted_without_explicit_amendment_resolution():
    amended = insider("AAPL", filing_form="4/A")
    out = scout_equities((), (market("AAPL"),), (amended,), decision_ns=NOW)
    assert out.candidates[0].insider_purchase_count == 0
    with pytest.raises(ValueError, match="transaction line"):
        insider("AAPL", transaction_line="")


def test_deterministic_ranking_independent_of_input_order():
    a, b = market("AAPL"), market("MSFT")
    f = insider("AAPL")
    one = scout_equities((), (a, b), (f,), decision_ns=NOW)
    two = scout_equities((), (b, a), (f,), decision_ns=NOW)
    assert one == two


def test_no_shortlist_truncation_into_order_authorization():
    pol = ScoutPolicy(max_candidates=1)
    out = scout_equities((), (market("MSFT"), market("AAPL")), (), decision_ns=NOW, policy=pol)
    assert len(out.candidates) == 1
    assert ("MSFT", "bounded research shortlist limit") in out.skipped
    assert not out.orders_authorized


def test_bad_policy_and_invalid_evidence_rejected():
    with pytest.raises(ValueError):
        ScoutPolicy(max_candidates=0)
    with pytest.raises(ValueError):
        market("AAPL", received_ns=NOW - DAY, session_ns=NOW)
    with pytest.raises(ValueError):
        market("AAPL", price=D("NaN"))
    with pytest.raises(ValueError):
        insider("AAPL", accession="fake", transaction_code="P")
    with pytest.raises(ValueError):
        insider("AAPL", source="openinsider.com")
    with pytest.raises(ValueError):
        scout_equities(("AAPL", "AAPL"), (), (), decision_ns=NOW)
