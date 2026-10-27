"""F1 isolated conservation invariants. No native broker/execution integration."""

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.economic_ledger import EconomicLedger, EconomicLedgerError, Posting


def D(s):
    return Decimal(s)


def test_exact_funding_buy_sell_fees_and_fifo_realized_gain():
    s = EconomicLedger().post_cash("fund", "CAPITAL_IN", "10000.00")
    s = s.post_fill("b1", "SPY", "BUY", "2", "100.00", commission="0.25")
    s = s.post_fill("b2", "SPY", "BUY", "2", "120.00")
    s = s.post_fill("s1", "SPY", "SELL", "3", "130.00", commission="0.75")
    s.audit()
    assert s.balance("cash:USD") == D("9949.00")
    assert s.balance("inventory:USD:SPY") == D("120")
    assert s.balance("income:realized") == D("-70")
    assert s.balance("expense:commission") == D("1")
    assert s.balance("equity:capital") == D("-10000")
    assert s.position("SPY") == D("1")
    assert [(lot.quantity, lot.basis) for lot in s.lots] == [(D("1"), D("120"))]
    assert len(s.journal) == 4


def test_short_open_partial_cover_and_cross_to_long():
    s = EconomicLedger().post_fill("short", "SPY", "SELL", "3", "100")
    assert s.position("SPY") == D("-3")
    assert s.balance("inventory:USD:SPY") == D("-300")
    s = s.post_fill("cover", "SPY", "BUY", "2", "80")
    assert s.balance("income:realized") == D("-40")
    assert s.balance("cash:USD") == D("140")
    s = s.post_fill("cross", "SPY", "BUY", "2", "90")
    assert s.position("SPY") == D("1")
    assert s.balance("inventory:USD:SPY") == D("90")
    assert s.balance("income:realized") == D("-50")
    assert s.balance("cash:USD") == D("-40")
    s.audit()


def test_close_short_at_loss_realizes_negative_profit():
    s = EconomicLedger().post_fill("short", "SPY", "SELL", "1", "10")
    s = s.post_fill("cover", "SPY", "BUY", "1", "15")
    assert s.position("SPY") == 0
    assert s.balance("cash:USD") == D("-5")
    assert s.balance("income:realized") == D("5")
    assert not s.lots


def test_dividends_and_financing_are_external_cash_flows_not_fills():
    s = EconomicLedger().post_cash("in", "CAPITAL_IN", "200")
    s = s.post_cash("div", "DIVIDEND", "3.50")
    s = s.post_cash("interest", "FINANCING", "1.25")
    s = s.post_cash("borrow", "BORROW_FEE", "0.50")
    s = s.post_cash("out", "CAPITAL_OUT", "10")
    assert s.balance("cash:USD") == D("191.75")
    assert s.balance("income:dividend") == D("-3.50")
    assert s.balance("expense:financing") == D("1.25")
    assert s.balance("expense:borrow") == D("0.50")
    assert s.balance("equity:capital") == D("-190")
    assert all(e.kind != "FILL" for e in s.journal)
    s.audit()


def test_split_revises_only_quantity_preserving_cost_basis():
    s = EconomicLedger().post_fill("buy", "SPY", "BUY", "3", "100")
    s2 = s.post_split("split", "SPY", 3, 2)
    assert s.position("SPY") == D("3")
    assert s2.position("SPY") == D("4.5")
    assert s2.balance("cash:USD") == s.balance("cash:USD")
    assert s2.balance("inventory:USD:SPY") == s.balance("inventory:USD:SPY")
    assert not s2.journal[-1].postings
    s3 = s2.post_fill("sell", "SPY", "SELL", "1.5", "90")
    assert s3.balance("income:realized") == D("-35")
    assert s3.position("SPY") == D("3")
    s3.audit()


def test_unrepresentable_split_ratio_fails_instead_of_hidden_rounding():
    s = EconomicLedger().post_fill("buy", "SPY", "BUY", "1", "101")
    with pytest.raises(EconomicLedgerError, match="rounding policy"):
        s.post_split("split", "SPY", 1, 3)
    assert len(s.journal) == 1


@pytest.mark.parametrize("invalid", [0.1, True, float("nan"), float("inf"), "NaN", "Infinity", "-1", "0"])
def test_reject_inexact_or_nonpositive_fill_prices(invalid):
    with pytest.raises(EconomicLedgerError):
        EconomicLedger().post_fill("buy", "SPY", "BUY", "1", invalid)


def test_duplicate_trade_and_foreign_side_fail_without_mutation():
    s = EconomicLedger().post_fill("f", "SPY", "BUY", "1", "100")
    with pytest.raises(EconomicLedgerError, match="duplicate"):
        s.post_cash("f", "DIVIDEND", "10")
    with pytest.raises(EconomicLedgerError, match="side"):
        s.post_fill("x", "SPY", "HOLD", "1", "10")
    assert len(s.journal) == 1


def test_tampered_postings_hash_or_lot_detected():
    s = EconomicLedger().post_fill("f", "SPY", "BUY", "1", "100")
    entry = s.journal[0]
    for altered in (
        replace(entry, sha256="0" * 64),
        replace(entry, postings=(Posting("cash:USD", D("999")),)),
    ):
        with pytest.raises(EconomicLedgerError):
            replace(s, journal=(altered,)).audit()
    with pytest.raises(EconomicLedgerError, match="inventory"):
        replace(s, lots=(replace(s.lots[0], basis=D("99")),)).audit()


def test_deterministic_byte_identical_chain_and_float_refusal():
    def replay():
        return (
            EconomicLedger()
            .post_cash("capital", "CAPITAL_IN", "1000")
            .post_fill("b", "SPY", "BUY", "0.5", "111.125", commission="0.003")
            .post_split("s", "SPY", 2, 1)
            .post_fill("c", "SPY", "SELL", "1", "56.5625")
        )

    first, second = replay(), replay()
    assert first == second
    assert [e.sha256 for e in first.journal] == [e.sha256 for e in second.journal]
    assert first.balance("income:realized") == D("-1")
    with pytest.raises(EconomicLedgerError):
        EconomicLedger().post_cash("x", "DIVIDEND", 1.1)


def test_partial_cost_basis_cannot_silently_round_repeating_fraction():
    s = EconomicLedger().post_fill("buy", "SPY", "BUY", "2", "0.01").post_split("split", "SPY", 3, 2)
    with pytest.raises(EconomicLedgerError, match="rounding policy"):
        s.post_fill("sell", "SPY", "SELL", "1", "2")
    assert s.position("SPY") == D("3")


def test_opened_lots_are_immutable_and_ownership_never_mutates():
    a = EconomicLedger()
    b = a.post_fill("b", "SPY", "BUY", "1", "100")
    assert not a.journal and not a.lots
    assert len(b.journal) == 1
    assert b.position("SPY") == D("1")


def test_multi_symbol_book_inventory_and_cash_balance_independently():
    s = EconomicLedger()
    s = s.post_fill("a", "SPY", "BUY", "1", "500")
    s = s.post_fill("b", "QQQ", "SELL", "2", "300")
    s = s.post_fill("c", "SPY", "SELL", "1", "520")
    assert s.balance("inventory:USD:QQQ") == D("-600")
    assert s.balance("inventory:USD:SPY") == 0
    assert s.balance("income:realized") == D("-20")
    assert s.balance("cash:USD") == D("620")
    s.audit()


def test_full_replay_detects_quantity_tamper_even_with_balanced_inventory_value():
    s = EconomicLedger().post_fill("buy", "SPY", "BUY", "1", "100")
    forged = replace(s, lots=(replace(s.lots[0], quantity=D("2")),))
    # The original postings are still balanced and inventory cost is still 100.
    # The final holding is nevertheless impossible from the transaction record.
    with pytest.raises(EconomicLedgerError, match="quantities"):
        forged.audit()


def test_full_replay_detects_forged_fill_economics_with_consistent_hash():
    from lab.economic_ledger import _digest

    s = EconomicLedger().post_fill("buy", "SPY", "BUY", "1", "100")
    entry = s.journal[0]
    # Rewrite all postings into a different, still balanced trade with the
    # same details, and recompute the unkeyed SHA to mimic local file tampering.
    altered = replace(
        entry,
        postings=(
            Posting("cash:USD", D("-90")),
            Posting("inventory:USD:SPY", D("90")),
            Posting("income:realized", D("0")),
        ),
    )
    altered = replace(altered, sha256=_digest("0" * 64, altered))
    with pytest.raises(EconomicLedgerError):
        replace(s, journal=(altered,), lots=(replace(s.lots[0], basis=D("90")),)).audit()


def test_multi_entry_replay_has_no_undocumented_synthetic_trade():
    s = (
        EconomicLedger()
        .post_cash("deposit", "CAPITAL_IN", "1000")
        .post_fill("buy", "SPY", "BUY", "2", "50")
        .post_split("split", "SPY", 2, 1)
        .post_cash("div", "DIVIDEND", "1.23")
        .post_fill("sell", "SPY", "SELL", "3", "26")
    )
    s.audit()
    assert s.position("SPY") == D("1")
    assert len([e for e in s.journal if e.kind == "FILL"]) == 2


def test_ambient_decimal_context_cannot_round_fifo_short_lot_or_fractional_settlement():
    from decimal import localcontext

    expected_qty = D("0.12345678901234567")
    with localcontext() as ctx:
        ctx.prec = 6
        short = EconomicLedger().post_fill("short", "SPY", "SELL", expected_qty, D("1.2"))
        assert short.position("SPY") == D("-0.12345678901234567")
        assert short.lots[0].quantity == D("-0.12345678901234567")
        short.audit()
        closed = short.post_cash_in_lieu("cil", "SPY", "SHORT", expected_qty, D("0.2"))
        closed.audit()
        assert closed.position("SPY") == 0
        assert not closed.lots
        assert closed.balance("cash:USD") == D("-0.051851853185185196")


def test_decimal_integer_digit_cap_is_exact_not_off_by_one():
    # 10**25 has twenty-six integer digits even though its coefficient is "1".
    with pytest.raises(EconomicLedgerError):
        EconomicLedger().post_cash("too-large", "CAPITAL_IN", D("1E+25"))
    s = EconomicLedger().post_cash("maximum", "CAPITAL_IN", D("1E+24"))
    s.audit()
    assert s.balance("cash:USD") == D("1E+24")
