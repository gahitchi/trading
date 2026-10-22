"""Borrow history is point-in-time and cannot authorize a short without evidence."""

from datetime import date, timedelta

import pytest

from lab.borrow import BorrowBook, BorrowQuote, BorrowUnavailable


def q(day, apr=0.08, available=True, ticker="GOOG", source="Vendor loan book"):
    return BorrowQuote(ticker, day, available, apr, source)


def test_lookups_are_asof_never_future_and_fail_on_staleness():
    base = date(2026, 10, 19)
    book = BorrowBook([q(base), q(base + timedelta(days=4), apr=0.3)])
    a, proof = book.borrow_rate("GOOG", base + timedelta(days=2))
    assert a == 0.08 and proof["as_of"] == base.isoformat() and proof["age_days"] == 2
    with pytest.raises(BorrowUnavailable, match="stale"):
        book.borrow_rate("GOOG", base + timedelta(days=5))
    with pytest.raises(BorrowUnavailable, match="no historical"):
        book.borrow_rate("GOOG", base - timedelta(days=1))


def test_no_locate_or_fee_history_blocks_qualified_short():
    day = date(2026, 10, 19)
    with pytest.raises(BorrowUnavailable, match="unavailable"):
        BorrowBook([q(day, apr=None, available=False)]).borrow_rate("GOOG", day)
    with pytest.raises(BorrowUnavailable, match="no historical"):
        BorrowBook([]).borrow_rate("GOOG", day)
    with pytest.raises(ValueError, match="observed APR"):
        q(day, apr=None, available=True)
    with pytest.raises(ValueError, match="non-assumed"):
        q(day, source="assumed")


def test_short_accrual_and_source_provenance():
    day = date(2026, 10, 19)
    b = BorrowBook([q(day, apr=0.12)])
    cost, evidence = b.cost({"GOOG": -10.0, "AAPL": 3.0}, {"GOOG": 100, "AAPL": 50}, day=day, holding_days=10)
    assert cost == pytest.approx(1000 * 0.12 * 10 / 365)
    assert evidence["GOOG"]["source"] == "Vendor loan book"
    assert "AAPL" not in evidence
    with pytest.raises(BorrowUnavailable, match="valid mark"):
        b.cost({"GOOG": -10.0}, {}, day=day, holding_days=10)


def test_csv_is_strict_and_reproducible(tmp_path):
    p = tmp_path / "loan.csv"
    p.write_text("ticker,as_of,available,apr,source\nGOOG,2026-10-19,true,0.35,IBKR loan desk\n")
    rate, proof = BorrowBook.from_csv(p).borrow_rate("GOOG", date(2026, 10, 19))
    assert rate == 0.35 and proof["source"] == "IBKR loan desk"
    with pytest.raises(ValueError, match="duplicate"):
        BorrowBook([q(date(2026, 10, 19)), q(date(2026, 10, 19))])


def test_verified_borrow_fee_overrides_uniform_estimate_only_when_supplied():
    from lab.config import MarginSettings
    from lab.margin import financing_cost

    margin = MarginSettings()
    assumed, _ = financing_cost(3000, 1000, 7, margin)
    verified, debit = financing_cost(3000, 1000, 7, margin, verified_borrow_fee=13.75)
    assert verified == 13.75 and debit == 0
    assert assumed == pytest.approx(1000 * margin.borrow_apr * 7 / 365)
    with pytest.raises(ValueError, match="nonnegative"):
        financing_cost(3000, 1000, 7, margin, verified_borrow_fee=-1)
