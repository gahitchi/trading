"""F1.1 read-only realized/unrealized USD reference valuation."""

import hashlib
import json
from decimal import Decimal

import pytest

from lab.economic_ledger import EconomicLedger
from lab.engine_events import EngineEconomicBook, EngineFactError, FillFact
from lab.reference_valuation import (
    parse_reference_mark_statement,
    value_economic_book,
)


def book():
    state = EngineEconomicBook("SIM-001")
    for i, symbol, side, qty, price in (
        (1, "SPY", "BUY", "2", "100"),
        (2, "QQQ", "SELL", "3", "200"),
        (3, "SPY", "SELL", "1", "110"),
    ):
        state = state.append(
            FillFact(f"fill-{i}", f"simulated:{i}", i, symbol, side, Decimal(qty), Decimal(price), Decimal("1"))
        )
    return state


def source(*, when=10, account="SIM-001", scope=True, currency="USD", marks=None):
    if marks is None:
        marks = [
            {"symbol": "SPY", "price": "120", "observed_at_ns": when},
            {"symbol": "QQQ", "price": "190", "observed_at_ns": when},
        ]
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": account,
            "record_id": "external-quotes-1",
            "as_of_ns": when,
            "source_kind": "INDEPENDENT_REFERENCE_MARKS",
            "quote_currency": currency,
            "complete_instrument_scope": scope,
            "marks": marks,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def parse(raw):
    return parse_reference_mark_statement(raw, source_namespace="vendor", expected_account_id="SIM-001")


def test_long_short_realized_and_unrealized_exact_without_economic_event():
    state = book()
    original = state.ledger
    raw = source()
    value = value_economic_book(state, parse(raw), max_mark_age_ns=0)
    assert value.statement_sha256 == hashlib.sha256(raw).hexdigest()
    assert value.cash_usd == Decimal("507")
    assert value.signed_marked_position_value == Decimal("-450")
    assert value.net_marked_equity_usd == Decimal("57")
    assert value.realized_profit_usd == Decimal("10")
    assert value.unrealized_profit_usd == Decimal("50")
    assert [(x.symbol, x.unrealized_profit) for x in value.positions] == [
        ("QQQ", Decimal("30")),
        ("SPY", Decimal("20")),
    ]
    assert state.ledger == original
    assert len(state.events) == 3
    assert not value.broker_source_authenticated
    assert not value.executable_market_price
    assert not value.trading_authorized


@pytest.mark.parametrize(
    "raw",
    [
        source(account="OTHER"),
        source(scope=False),
        source(currency="EUR"),
        source(marks=[{"symbol": "SPY", "price": "120", "observed_at_ns": 10}]),
        source(
            marks=[
                {"symbol": "SPY", "price": "120", "observed_at_ns": 10},
                {"symbol": "SPY", "price": "130", "observed_at_ns": 10},
            ]
        ),
        source(
            marks=[
                {"symbol": "SPY", "price": "NaN", "observed_at_ns": 10},
                {"symbol": "QQQ", "price": "190", "observed_at_ns": 10},
            ]
        ),
        source(
            marks=[
                {"symbol": "SPY", "price": "1E+99999", "observed_at_ns": 10},
                {"symbol": "QQQ", "price": "190", "observed_at_ns": 10},
            ]
        ),
        source(
            marks=[
                {"symbol": "SPY", "price": "120", "observed_at_ns": 11},
                {"symbol": "QQQ", "price": "190", "observed_at_ns": 10},
            ]
        ),
    ],
)
def test_invalid_or_incomplete_source_is_refused(raw):
    with pytest.raises(EngineFactError):
        report = parse(raw)
        value_economic_book(book(), report, max_mark_age_ns=0)


def test_explicit_freshness_bound_is_required_and_enforced():
    marks = [
        {"symbol": "SPY", "price": "120", "observed_at_ns": 8},
        {"symbol": "QQQ", "price": "190", "observed_at_ns": 9},
    ]
    report = parse(source(when=10, marks=marks))
    with pytest.raises(EngineFactError, match="stale"):
        value_economic_book(book(), report, max_mark_age_ns=1)
    assert value_economic_book(book(), report, max_mark_age_ns=2).complete_valuations_checked


def test_no_position_allows_complete_empty_marks_without_inventing_profit():
    state = EngineEconomicBook("SIM-001")
    report = parse(source(marks=[]))
    value = value_economic_book(state, report, max_mark_age_ns=0)
    assert not value.positions
    assert value.net_marked_equity_usd == 0
    assert value.realized_profit_usd == 0
    assert value.unrealized_profit_usd == 0


def test_untrusted_mutated_typed_mark_rejected():
    from dataclasses import replace

    report = parse(source())
    changed = replace(report, marks=(replace(report.marks[0], price=Decimal("NaN")), report.marks[1]))
    with pytest.raises((EngineFactError, ValueError)):
        value_economic_book(book(), changed, max_mark_age_ns=0)


def test_realized_profit_matches_original_fifo_journal_not_reference_marks():
    ledger = EconomicLedger().post_fill("open", "SPY", "BUY", "2", "100", commission="1")
    ledger = ledger.post_fill("close", "SPY", "SELL", "2", "130", commission="1")
    assert ledger.balance("income:realized") == Decimal("-60")


def test_source_digest_cannot_be_reused_with_valid_but_mutated_valuation():
    from dataclasses import replace

    report = parse(source())
    modified = replace(
        report,
        marks=(replace(report.marks[0], price=Decimal("140")), report.marks[1]),
    )
    with pytest.raises(EngineFactError, match="original evidence bytes"):
        value_economic_book(book(), modified, max_mark_age_ns=0)


def test_valuation_refuses_large_exact_inputs_whose_product_exceeds_supported_precision():
    huge = "9" * 25
    report = parse(
        source(
            marks=[
                {"symbol": "SPY", "price": "120", "observed_at_ns": 10},
                {"symbol": "QQQ", "price": huge, "observed_at_ns": 10},
            ]
        )
    )
    with pytest.raises(EngineFactError, match="bounded"):
        value_economic_book(book(), report, max_mark_age_ns=0)
