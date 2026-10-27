"""Independent rational account projection against exact double-entry economics."""

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.account_projection import compare_projection, project_account
from lab.engine_events import CashFact, EngineEconomicBook, EngineFactError, FillFact, SplitFact


def D(text):
    return Decimal(text)


def cash(kind, amount, time):
    return CashFact(
        f"c{time}",
        f"statement:cash:{time}",
        time,
        kind,
        D(amount),
        "BROKER_CASH_ACTIVITY",
        "SETTLED",
        "a" * 64,
    )


def fill(id_, time, side, quantity, price, commission="0.25"):
    return FillFact(id_, f"trade:{id_}", time, "SPY", side, D(quantity), D(price), D(commission))


def test_independent_projection_handles_partial_close_splits_and_real_cash_settlements():
    book = EngineEconomicBook("SIM-001")
    book = book.append(cash("CAPITAL_IN", "1000", 1))
    book = book.append(fill("buy1", 2, "BUY", "2", "100"))
    book = book.append(SplitFact("split1", "action:split", 3, "SPY", 2, 1))
    book = book.append(fill("sell1", 4, "SELL", "2", "130"))
    book = book.append(cash("DIVIDEND", "3.50", 5))
    book = book.append(cash("FINANCING", "1.20", 6))
    book = book.append(cash("BORROW_FEE", "0.30", 7))
    projected = project_account(book)
    assert projected.cash == D("1061.50")
    assert projected.quantity("SPY") == D("2")
    assert projected.source_event_ids == tuple(event.event_id for event in book.events)
    parity = compare_projection(book)
    assert parity.matches
    assert parity.cash_delta == 0
    assert parity.position_deltas == (("SPY", D("0")),)
    assert parity.events_checked == 7


def test_independent_projection_handles_shorts_crosses_and_cash_out():
    book = EngineEconomicBook("SIM-001").append(fill("short", 1, "SELL", "3", "100"))
    book = book.append(fill("cover", 2, "BUY", "2", "80"))
    assert project_account(book).cash == D("139.50")
    assert project_account(book).quantity("SPY") == D("-1")
    book = book.append(fill("cross", 3, "BUY", "2", "90"))
    assert project_account(book).quantity("SPY") == D("1")
    assert compare_projection(book).matches


def test_empty_account_and_all_flat_positions_can_be_replayed():
    empty = EngineEconomicBook("SIM-001")
    assert project_account(empty).cash == 0
    assert not project_account(empty).positions
    book = empty.append(fill("buy", 1, "BUY", "1", "100", "0"))
    book = book.append(fill("sell", 2, "SELL", "1", "101", "0"))
    assert not project_account(book).positions
    assert project_account(book).cash == D("1")
    assert compare_projection(book).matches


def test_independently_projected_fractional_amounts_are_never_rounded():
    book = EngineEconomicBook("SIM-001").append(fill("buy", 1, "BUY", "0.125", "1.0001", "0.00001"))
    assert project_account(book).cash == D("-0.1250225")
    assert compare_projection(book).matches


def test_projection_refuses_mutated_source_or_ledger():
    book = EngineEconomicBook("SIM-001").append(cash("CAPITAL_IN", "100", 1))
    damaged = replace(book, events=(replace(book.events[0], amount=D("101")),))
    with pytest.raises(EngineFactError):
        project_account(damaged)
    with pytest.raises(EngineFactError):
        compare_projection(damaged)


def test_projected_cash_precision_does_not_depend_on_ambient_decimal_context():
    from decimal import localcontext

    book = EngineEconomicBook("SIM-001").append(cash("CAPITAL_IN", "1234567890123456789.12345", 1))
    with localcontext() as context:
        context.prec = 6
        # Independently derived exact-value construction is context agnostic.
        assert project_account(book).cash == D("1234567890123456789.12345")
