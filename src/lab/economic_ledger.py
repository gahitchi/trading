"""F1 prototype: exact USD double-entry accounting with immutable FIFO lots.

This module is deliberately NOT wired to BacktestEngine, ExecutionAgent or a
broker. F0 and F1 qualification remain blocked. Native account/portfolio
settlement parity requires a separate adapter and external evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException, Inexact, Rounded, localcontext

SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}\Z")
# Each non-trade settled cash movement must name its economic counterpart.
# Debits to cash are positive; income and capital carry opposite postings.
CASH_POSTINGS = {
    "CAPITAL_IN": ("equity:capital", 1),
    "CAPITAL_OUT": ("equity:capital", -1),
    "DIVIDEND": ("income:dividend", 1),
    "INTEREST_CREDIT": ("income:interest", 1),
    "BORROW_REBATE": ("income:borrow_rebate", 1),
    "DIVIDEND_WITHHOLDING": ("expense:withholding", -1),
    "SHORT_DISTRIBUTION": ("expense:short_distribution", -1),
    "FINANCING": ("expense:financing", -1),
    "BORROW_FEE": ("expense:borrow", -1),
}
KINDS = frozenset(CASH_POSTINGS)
MAX_DIGITS = 25
MAX_SCALE = 18


class EconomicLedgerError(ValueError):
    """A nonconserving, inexact, ambiguous or unsupported event was refused."""


def exact(value: Decimal | str | int, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    """Use exact decimal inputs; binary floats never cross the economic boundary."""
    if type(value) is Decimal:
        number = value
    elif type(value) in (str, int):
        try:
            number = Decimal(value)
        except DecimalException as exc:
            raise EconomicLedgerError("invalid decimal amount") from exc
    else:
        raise EconomicLedgerError("monetary values must not use floats or booleans")
    if (
        not number.is_finite()
        or len(number.as_tuple().digits) > MAX_DIGITS
        or number.as_tuple().exponent < -MAX_SCALE
        or number.adjusted() >= MAX_DIGITS
        or (positive and number <= 0)
        or (nonnegative and number < 0)
    ):
        raise EconomicLedgerError("out-of-range, negative, or nonfinite decimal")
    return Decimal(0) if number == 0 else number


def _name(value: str, pattern: re.Pattern[str], description: str) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise EconomicLedgerError(f"invalid {description}")
    return value


def _calc(fn):
    """Trap *all* hidden rounding, including FIFO proportional cost release."""
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            return fn()
    except DecimalException as exc:
        raise EconomicLedgerError("economic calculation requires explicit rounding policy") from exc


def _fmt(amount: Decimal) -> str:
    return format(amount, "f")


@dataclass(frozen=True, slots=True)
class Posting:
    account: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Lot:
    symbol: str
    quantity: Decimal  # positive long, negative short
    basis: Decimal  # signed book value; shorts are liabilities


@dataclass(frozen=True, slots=True)
class JournalEntry:
    event_id: str
    kind: str
    postings: tuple[Posting, ...]
    details: tuple[tuple[str, str], ...]
    sha256: str


def _digest(previous: str, event: JournalEntry) -> str:
    document = {
        "previous": previous,
        "id": event.event_id,
        "kind": event.kind,
        "postings": [[p.account, _fmt(p.amount)] for p in event.postings],
        "details": list(event.details),
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EconomicLedger:
    """An isolated accounting oracle; not an executable engine or cash authority."""

    journal: tuple[JournalEntry, ...] = ()
    lots: tuple[Lot, ...] = ()

    def balance(self, account: str) -> Decimal:
        return _calc(
            lambda: sum((p.amount for e in self.journal for p in e.postings if p.account == account), Decimal(0))
        )

    def position(self, symbol: str) -> Decimal:
        symbol = _name(symbol, SYMBOL, "symbol")
        return _calc(lambda: sum((lot.quantity for lot in self.lots if lot.symbol == symbol), Decimal(0)))

    def _post(
        self,
        event_id: str,
        kind: str,
        postings: tuple[Posting, ...],
        details: tuple[tuple[str, str], ...],
        lots: tuple[Lot, ...],
    ) -> EconomicLedger:
        _name(event_id, IDENTIFIER, "event identity")
        if any(event.event_id == event_id for event in self.journal):
            raise EconomicLedgerError("duplicate economic event identity")
        if kind == "SPLIT":
            if postings:
                raise EconomicLedgerError("share splits cannot manufacture cash or trading volume")
        elif not postings or any(type(p) is not Posting for p in postings):
            raise EconomicLedgerError("monetary event requires typed postings")
        if any(type(p.account) is not str or not p.account or type(p.amount) is not Decimal for p in postings):
            raise EconomicLedgerError("invalid journal posting")
        if _calc(lambda: sum((p.amount for p in postings), Decimal(0))) != 0:
            raise EconomicLedgerError("unbalanced economic journal")
        next_entry = JournalEntry(event_id, kind, postings, details, "")
        prior = self.journal[-1].sha256 if self.journal else "0" * 64
        next_entry = replace(next_entry, sha256=_digest(prior, next_entry))
        candidate = EconomicLedger((*self.journal, next_entry), lots)
        candidate._audit_postings()
        return candidate

    def post_cash(self, event_id: str, kind: str, amount: Decimal | str | int) -> EconomicLedger:
        if kind not in KINDS:
            raise EconomicLedgerError("unsupported non-trade settlement event")
        value = exact(amount, positive=True)
        account, direction = CASH_POSTINGS[kind]
        debit = _calc(lambda: value * direction)
        return self._post(
            event_id,
            kind,
            (Posting("cash:USD", debit), Posting(account, _calc(lambda: -debit))),
            (("amount", _fmt(value)),),
            self.lots,
        )

    def post_cash_reversal(
        self,
        event_id: str,
        target_event_id: str,
        original_kind: str,
        amount: Decimal | str | int,
    ) -> EconomicLedger:
        """Compensate one *settled cash* posting without altering its original.

        Trade corrections and corporate-action corrections are not reducible
        to cash reversals. They must go through separate, reviewed policies.
        """
        _name(target_event_id, IDENTIFIER, "reversed event identity")
        if original_kind not in KINDS:
            raise EconomicLedgerError("cash reversal cannot target noncash economics")
        quantity = exact(amount, positive=True)
        match = [entry for entry in self.journal if entry.event_id == target_event_id]
        if len(match) != 1:
            raise EconomicLedgerError("cash reversal target must exist in this account")
        original = match[0]
        if original.kind != original_kind or original.details != (("amount", _fmt(quantity)),):
            raise EconomicLedgerError("cash reversal does not match original settled economics")
        if any(
            entry.kind == "CASH_REVERSAL" and dict(entry.details).get("target_event_id") == target_event_id
            for entry in self.journal
        ):
            raise EconomicLedgerError("settled cash event already reversed")
        postings = tuple(Posting(item.account, _calc(lambda item=item: -item.amount)) for item in original.postings)
        return self._post(
            event_id,
            "CASH_REVERSAL",
            postings,
            (("target_event_id", target_event_id), ("original_kind", original_kind), ("amount", _fmt(quantity))),
            self.lots,
        )

    def post_fill(
        self,
        event_id: str,
        symbol: str,
        side: str,
        quantity: Decimal | str | int,
        price: Decimal | str | int,
        *,
        commission: Decimal | str | int = "0",
    ) -> EconomicLedger:
        symbol = _name(symbol, SYMBOL, "symbol")
        if side not in ("BUY", "SELL"):
            raise EconomicLedgerError("fill side must be BUY or SELL")
        qty = exact(quantity, positive=True)
        px = exact(price, positive=True)
        fee = exact(commission, nonnegative=True)
        signed = qty if side == "BUY" else _calc(lambda: -qty)
        remaining = qty
        lots: list[Lot] = []
        inventory_delta = Decimal(0)

        for lot in self.lots:
            if lot.symbol != symbol or (lot.quantity > 0) == (signed > 0) or remaining == 0:
                lots.append(lot)
                continue
            close = min(lot.quantity.copy_abs(), remaining)
            released = _calc(lambda lot=lot, close=close: lot.basis * close / abs(lot.quantity))
            inventory_delta = _calc(
                lambda inventory_delta=inventory_delta, released=released: inventory_delta - released
            )
            updated_qty = _calc(lambda lot=lot, close=close: lot.quantity + (close if signed > 0 else -close))
            updated_basis = _calc(lambda lot=lot, released=released: lot.basis - released)
            if updated_qty:
                lots.append(Lot(lot.symbol, updated_qty, updated_basis))
            remaining = _calc(lambda remaining=remaining, close=close: remaining - close)

        if remaining:
            opened_qty = remaining if side == "BUY" else _calc(lambda: -remaining)
            basis = _calc(lambda: opened_qty * px)
            lots.append(Lot(symbol, opened_qty, basis))
            inventory_delta = _calc(lambda: inventory_delta + basis)
        cash_trade = _calc(lambda: -signed * px)
        realized_entry = _calc(lambda: -(cash_trade + inventory_delta))
        cash_delta = _calc(lambda: cash_trade - fee)
        postings = [
            Posting("cash:USD", cash_delta),
            Posting(f"inventory:USD:{symbol}", inventory_delta),
            Posting("income:realized", realized_entry),
        ]
        if fee:
            postings.append(Posting("expense:commission", fee))
        return self._post(
            event_id,
            "FILL",
            tuple(postings),
            (
                ("symbol", symbol),
                ("side", side),
                ("quantity", _fmt(qty)),
                ("price", _fmt(px)),
                ("commission", _fmt(fee)),
            ),
            tuple(lots),
        )

    def post_split(self, event_id: str, symbol: str, numerator: int, denominator: int) -> EconomicLedger:
        """Non-trade share transform. Basis/cash stay unchanged; never invent a fill."""
        symbol = _name(symbol, SYMBOL, "symbol")
        if (
            type(numerator) is not int
            or type(denominator) is not int
            or not 1 <= numerator <= 1_000_000
            or not 1 <= denominator <= 1_000_000
        ):
            raise EconomicLedgerError("split ratio must be finite positive integers")
        if not any(lot.symbol == symbol for lot in self.lots):
            raise EconomicLedgerError("split cannot create a position")
        lots = []
        for lot in self.lots:
            if lot.symbol == symbol:
                new_qty = _calc(lambda lot=lot: (lot.quantity * Decimal(numerator)) / Decimal(denominator))
                new_qty = exact(new_qty, positive=False)
                if new_qty == 0:
                    raise EconomicLedgerError("split removed a position")
                lots.append(Lot(symbol, new_qty, lot.basis))
            else:
                lots.append(lot)
        return self._post(
            event_id,
            "SPLIT",
            (),
            (("symbol", symbol), ("numerator", str(numerator)), ("denominator", str(denominator))),
            tuple(lots),
        )

    def post_cash_in_lieu(
        self,
        event_id: str,
        symbol: str,
        side: str,
        quantity: Decimal | str | int,
        cash: Decimal | str | int,
    ) -> EconomicLedger:
        """Settle fractional shares without inventing an exchange fill.

        The cash is an externally claimed settled gross receipt (LONG) or
        payment (SHORT). Withholding and fees require separate typed events.
        Cost is released FIFO, with no implicit rounding of fraction or basis.
        """
        symbol = _name(symbol, SYMBOL, "symbol")
        if side not in ("LONG", "SHORT"):
            raise EconomicLedgerError("cash-in-lieu side must identify long or short position")
        qty = exact(quantity, positive=True)
        if qty >= 1:
            raise EconomicLedgerError("cash-in-lieu is restricted to fractional shares")
        paid = exact(cash, positive=True)
        remaining = qty
        lots: list[Lot] = []
        released_total = Decimal(0)
        for lot in self.lots:
            correct_side = (lot.quantity > 0) if side == "LONG" else (lot.quantity < 0)
            if lot.symbol != symbol or not correct_side or remaining == 0:
                lots.append(lot)
                continue
            close = min(lot.quantity.copy_abs(), remaining)
            released = _calc(lambda lot=lot, close=close: lot.basis * close / abs(lot.quantity))
            released_total = _calc(lambda released_total=released_total, released=released: released_total + released)
            remaining = _calc(lambda remaining=remaining, close=close: remaining - close)
            next_qty = _calc(
                lambda lot=lot, close=close: lot.quantity - close if side == "LONG" else lot.quantity + close
            )
            next_basis = _calc(lambda lot=lot, released=released: lot.basis - released)
            if next_qty:
                lots.append(Lot(symbol, next_qty, next_basis))
        if remaining:
            raise EconomicLedgerError("cash-in-lieu exceeds eligible fractional holdings")
        cash_delta = paid if side == "LONG" else _calc(lambda: -paid)
        inventory_delta = _calc(lambda: -released_total)
        realized = _calc(lambda: -(cash_delta + inventory_delta))
        return self._post(
            event_id,
            "CASH_IN_LIEU",
            (
                Posting("cash:USD", cash_delta),
                Posting(f"inventory:USD:{symbol}", inventory_delta),
                Posting("income:realized", realized),
            ),
            (
                ("symbol", symbol),
                ("side", side),
                ("quantity", _fmt(qty)),
                ("cash", _fmt(paid)),
            ),
            tuple(lots),
        )

    def _audit_postings(self) -> None:
        """Check each double-entry transaction, custody chain and ending inventory value."""
        seen: set[str] = set()
        prev = "0" * 64
        balances: dict[str, Decimal] = {}
        for entry in self.journal:
            _name(entry.event_id, IDENTIFIER, "event identity")
            if entry.event_id in seen or entry.sha256 != _digest(prev, entry):
                raise EconomicLedgerError("duplicate event or corrupted journal chain")
            seen.add(entry.event_id)
            prev = entry.sha256
            if entry.kind == "SPLIT":
                if entry.postings:
                    raise EconomicLedgerError("split cannot contain monetary postings")
            elif not entry.postings:
                raise EconomicLedgerError("empty monetary journal entry")
            if _calc(lambda entry=entry: sum((p.amount for p in entry.postings), Decimal(0))) != 0:
                raise EconomicLedgerError("unbalanced historical event")
            for posting in entry.postings:
                balances[posting.account] = _calc(
                    lambda posting=posting: balances.get(posting.account, Decimal(0)) + posting.amount
                )
        symbols = {lot.symbol for lot in self.lots}
        symbols |= {account.split(":")[2] for account in balances if account.startswith("inventory:USD:")}
        for lot in self.lots:
            _name(lot.symbol, SYMBOL, "lot symbol")
            exact(lot.quantity)
            exact(lot.basis)
            if lot.quantity == 0 or lot.basis == 0 or (lot.quantity > 0) != (lot.basis > 0):
                raise EconomicLedgerError("invalid open lot sign or zero balance")
        for symbol in symbols:
            expected = _calc(
                lambda symbol=symbol: sum((lot.basis for lot in self.lots if lot.symbol == symbol), Decimal(0))
            )
            if balances.get(f"inventory:USD:{symbol}", Decimal(0)) != expected:
                raise EconomicLedgerError("inventory does not reconcile with FIFO cost basis")
        if _calc(lambda: sum(balances.values(), Decimal(0))) != 0:
            raise EconomicLedgerError("economic assets, capital, income and expense do not conserve")

    def audit(self) -> None:
        """Reconstruct all event economics independently from recorded business inputs.

        Hashes and a balanced journal alone do not demonstrate correct
        quantities, FIFO realization or entry semantics; reconstruct from
        the recorded event payload and compare each generated posting.
        """
        self._audit_postings()
        replay = EconomicLedger()
        for event in self.journal:
            details = dict(event.details)
            if len(details) != len(event.details) or any(
                type(pair) is not tuple or len(pair) != 2 or any(type(value) is not str for value in pair)
                for pair in event.details
            ):
                raise EconomicLedgerError("invalid or duplicate economic event details")
            try:
                if event.kind == "FILL":
                    if set(details) != {"symbol", "side", "quantity", "price", "commission"}:
                        raise EconomicLedgerError("fill event payload is incomplete")
                    replay = replay.post_fill(
                        event.event_id,
                        details["symbol"],
                        details["side"],
                        details["quantity"],
                        details["price"],
                        commission=details["commission"],
                    )
                elif event.kind == "SPLIT":
                    if set(details) != {"symbol", "numerator", "denominator"}:
                        raise EconomicLedgerError("split event payload is incomplete")
                    replay = replay.post_split(
                        event.event_id, details["symbol"], int(details["numerator"]), int(details["denominator"])
                    )
                elif event.kind == "CASH_IN_LIEU":
                    if set(details) != {"symbol", "side", "quantity", "cash"}:
                        raise EconomicLedgerError("cash-in-lieu payload is incomplete")
                    replay = replay.post_cash_in_lieu(
                        event.event_id, details["symbol"], details["side"], details["quantity"], details["cash"]
                    )
                elif event.kind in KINDS:
                    if set(details) != {"amount"}:
                        raise EconomicLedgerError("cash event payload is incomplete")
                    replay = replay.post_cash(event.event_id, event.kind, details["amount"])
                elif event.kind == "CASH_REVERSAL":
                    if set(details) != {"target_event_id", "original_kind", "amount"}:
                        raise EconomicLedgerError("cash reversal payload is incomplete")
                    replay = replay.post_cash_reversal(
                        event.event_id, details["target_event_id"], details["original_kind"], details["amount"]
                    )
                else:
                    raise EconomicLedgerError("unknown economic event in journal")
            except (KeyError, TypeError, ValueError) as exc:
                raise EconomicLedgerError("economic history cannot be replayed") from exc
            if replay.journal[-1] != event:
                raise EconomicLedgerError("recorded economic postings differ from deterministic replay")
        if replay.lots != self.lots:
            raise EconomicLedgerError("open position quantities or FIFO lots diverge from event replay")
