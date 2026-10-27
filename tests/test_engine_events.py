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


def cash(
    event_id="deposit",
    source_ref="ledger:deposit",
    time=1,
    kind="CAPITAL_IN",
    amount="1000",
    *,
    source_kind="BROKER_CASH_ACTIVITY",
    settlement_status="SETTLED",
    source_sha256="a" * 64,
):
    # A well-formed synthetic source fingerprint is not independent authenticity.
    return CashFact(event_id, source_ref, time, kind, d(amount), source_kind, settlement_status, source_sha256)


def fill(event_id="fill1", source_ref="trade:fill1", time=2, side="BUY", qty="2", px="100", fee="0.25"):
    return FillFact(event_id, source_ref, time, "SPY", side, d(qty), d(px), d(fee))


def observed(book, *, cash_value="799.75", positions=None, ids=None, as_of_ns=20, scope=True):
    if positions is None:
        positions = (EnginePosition("SPY", d("2")),)
    if ids is None:
        ids = tuple(event.event_id for event in book.events)
    return EngineSnapshot(book.account_id, as_of_ns, ids, d(cash_value), positions, scope, book.evidence_head)


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
    assert parity.evidence_head == bought.evidence_head


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
        replace(reference, applied_evidence_head="0" * 64),
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


def test_evidence_hash_binds_provenance_time_account_and_raw_amounts():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    book.audit()
    assert len(book.evidence_head) == 64
    assert book.evidence_head != "0" * 64
    assert EngineEconomicBook("SIM-001").append(cash()).append(fill()).evidence_head == book.evidence_head
    assert EngineEconomicBook("SIM-002").append(cash()).append(fill()).evidence_head != book.evidence_head
    assert EngineEconomicBook("SIM-001").append(replace(cash(), source_ref="bank:another")).evidence_head != (
        EngineEconomicBook("SIM-001").append(cash()).evidence_head
    )


@pytest.mark.parametrize(
    "altered",
    [
        replace(cash(), source_ref="alternate:source"),
        replace(cash(), timestamp_ns=2),
        replace(cash(), amount=d("999")),
    ],
)
def test_recomputed_business_journal_alone_cannot_hide_evidence_metadata_rewrite(altered):
    original = EngineEconomicBook("SIM-001").append(cash())
    modified = replace(original, events=(altered,))
    with pytest.raises(EngineFactError):
        modified.audit()


def test_snapshot_event_ids_are_not_sufficient_when_evidence_hash_disagrees():
    book = EngineEconomicBook("SIM-001").append(cash()).append(fill())
    snapshot = observed(book)
    assert book.reconcile(snapshot).matches
    with pytest.raises(EngineFactError, match="fingerprint"):
        book.reconcile(replace(snapshot, applied_evidence_head="f" * 64))


@pytest.mark.parametrize(
    "bad",
    [
        cash(settlement_status="ESTIMATED"),
        cash(settlement_status="PENDING"),
        cash(settlement_status="VERIFIED"),
        cash(settlement_status=True),
        cash(source_kind="UNCLASSIFIED"),
        cash(kind="FINANCING", source_kind="BANK_TRANSFER"),
        cash(kind="BORROW_FEE", source_kind="BANK_TRANSFER"),
        cash(kind="DIVIDEND", source_kind="BANK_TRANSFER"),
        cash(source_sha256=""),
        cash(source_sha256="A" * 64),
        cash(source_sha256="a" * 63),
        cash(source_sha256=None),
    ],
)
def test_unsettled_or_unclassified_cash_cannot_enter_engine_book(bad):
    original = EngineEconomicBook("SIM-001")
    with pytest.raises(EngineFactError):
        original.append(bad)
    assert not original.events
    assert not original.ledger.journal


def test_settled_dividend_and_financing_are_non_trade_cash_events():
    book = EngineEconomicBook("SIM-001").append(cash())
    book = book.append(cash("div", "statement:div", 2, "DIVIDEND", "3.50"))
    book = book.append(cash("carry", "statement:carry", 3, "FINANCING", "0.25"))
    book = book.append(cash("borrow", "statement:borrow", 4, "BORROW_FEE", "0.50"))
    book.audit()
    assert book.ledger.balance("cash:USD") == d("1002.75")
    assert all(entry.kind != "FILL" for entry in book.ledger.journal)


def test_settlement_source_hash_is_bound_to_engine_evidence_identity():
    book = EngineEconomicBook("SIM-001").append(cash())
    forged = replace(book, events=(replace(book.events[0], source_sha256="b" * 64),))
    with pytest.raises(EngineFactError, match="custody chain"):
        forged.audit()
    assert book.evidence_head != EngineEconomicBook("SIM-001").append(
        replace(cash(), source_sha256="b" * 64)
    ).evidence_head
