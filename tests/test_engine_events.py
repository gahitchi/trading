"""F1 independent-engine F0/F1.2 event boundary: no Nautilus or broker access."""

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.economic_ledger import EconomicLedger
from lab.engine_events import (
    CashFact,
    EngineEconomicBook,
    EngineFactError,
    EnginePosition,
    EngineSnapshot,
    FillFact,
    SplitFact,
)


def d(value):
    return Decimal(value)


def cash(event_id="deposit", source_ref="ledger:deposit", time=1, kind="CAPITAL_IN", amount="1000"):
    return CashFact(event_id, source_ref, time, kind, d(amount))


def fill(event_id="fill1", source_ref="trade:fill1", time=2, side="BUY", qty="2", px="100", fee="0.25"):
    return FillFact(event_id, source_ref, time, "SPY", side, d(qty), d(px), d(fee))


def observed(book, *, cash_value="799.75", positions=None, ids=None, as_of_ns=20, scope=True):
    if positions is None:
        positions = (EnginePosition("SPY", d("2")),)
    if ids is None:
        ids = tuple(event.event_id for event in book.events)
    return EngineSnapshot(book.account_id, as_of_ns, ids, d(cash_value), positions, scope)


def test_engine_event_book_is_owned_pure_decimal_immutable_and_replayable():
    empty = EngineEconomicBook("SIM-001")
    funded = empty.append(cash())
    bought = funded.append(fill())
    assert empty.events == ()
    assert funded.ledger.balance("cash:USD") == d("1000")
    assert bought.ledger.balance("cash:USD") == d("799.75")
    assert bought.ledger.position("SPY") == d("2")
    bought.audit()
    parity = bought.reconcile(observed(bought))
    assert parity.matches
    assert parity.position_deltas == (("SPY", d("0")),)


def test_engine_events_preserve_fifo_and_corporate_action_without_fake_trades():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    book = book.append(SplitFact("split", "action:2026-01", 3, "SPY", 2, 1))
    book = book.append(fill("fill2", "trade:fill2", 4, "SELL", "2", "130", "0.5"))
    book.audit()
    assert book.ledger.balance("cash:USD") == d("1059.25")
    assert book.ledger.position("SPY") == d("2")
    assert len([e for e in book.ledger.journal if e.kind == "FILL"]) == 2
    state = observed(book, cash_value="1059.25", positions=(EnginePosition("SPY", d("2")),))
    assert book.reconcile(state).matches


def test_short_sell_and_cover_do_not_assume_a_single_broker():
    book = EngineEconomicBook("SIM-001")
    book = book.append(fill("short", "trade:short", 1, "SELL", "2", "100", "0"))
    book = book.append(fill("cover", "trade:cover", 2, "BUY", "1", "80", "0"))
    book.audit()
    assert book.ledger.position("SPY") == d("-1")
    assert book.ledger.balance("cash:USD") == d("120")


def test_unknown_cash_delta_cannot_be_automatically_classified():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    result = book.reconcile(observed(book, cash_value="800.75"))
    assert result.cash_delta == d("1")
    assert not result.matches
    assert len(book.events) == 2
    with pytest.raises(EngineFactError, match="unclassified"):
        book.append(cash("unknown", "unknown:source", 3, kind="UNKNOWN_BALANCE_DELTA"))


@pytest.mark.parametrize(
    "bad",
    [
        replace(fill(), quantity=1.0),
        replace(fill(), commission=None),
        replace(fill(), price=d("NaN")),
        replace(fill(), quantity=d("0")),
        replace(fill(), side="CANCEL"),
        replace(fill(), source_ref=""),
        replace(fill(), timestamp_ns=True),
        replace(fill(), symbol="SPY/INVALID"),
    ],
)
def test_rejects_unsafe_business_events_without_mutation(bad):
    before = EngineEconomicBook("SIM-001")
    with pytest.raises(EngineFactError):
        before.append(bad)
    assert before.ledger == EconomicLedger()


def test_duplicate_source_event_and_reordered_economics_refused():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    for bad in (
        cash("deposit2", "ledger:deposit", 3),
        fill("fill1", "trade:different", 3),
        fill("late", "trade:late", 1),
    ):
        with pytest.raises(EngineFactError):
            book.append(bad)


def test_independent_state_comparison_fails_closed_on_missing_coverage_or_events():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    reference = observed(book)
    for bad in (
        replace(reference, complete_instrument_scope=False),
        replace(reference, applied_event_ids=("deposit",)),
        replace(reference, applied_event_ids=("fill1", "deposit")),
        replace(reference, as_of_ns=1),
        replace(reference, account_id="FOREIGN"),
        replace(reference, cash=d("799.750000001")),
        replace(reference, positions=(EnginePosition("SPY", d("1")),)),
    ):
        if bad.cash != reference.cash or bad.positions != reference.positions:
            assert not book.reconcile(bad).matches
        else:
            with pytest.raises(EngineFactError):
                book.reconcile(bad)


def test_snapshot_rejects_omitted_open_position_and_duplicate_instrument():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    for positions in (
        (),
        (EnginePosition("SPY", d("2")), EnginePosition("SPY", d("2"))),
        (EnginePosition("SPY", 2.0),),
    ):
        with pytest.raises(EngineFactError):
            book.reconcile(observed(book, positions=positions))


def test_tampered_ledger_refused_even_if_cash_and_positions_appear_correct():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    bad = replace(book, ledger=book.ledger.post_cash("forged", "CAPITAL_IN", "1"))
    with pytest.raises(EngineFactError, match="journal differs"):
        bad.audit()
