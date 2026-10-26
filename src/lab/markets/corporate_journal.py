"""Crash-replayable corporate-action reference ledger on existing EventJournal.

This is a standalone financial audit reference. It never creates Nautilus
orders, changes a broker account or mutates native positions. The complete
original independent book is required for replay and must be authenticated
separately before any future production integration.
"""

from __future__ import annotations

import hashlib

from lab.markets.corporate_actions import (
    CashReceipt,
    CashTermination,
    CorporateActionError,
    CorporateBook,
    ReconciledBook,
    _digest,
    receive,
    recognize,
)
from lab.markets.journal import EventJournal, JournalError

_SESSION = "session:corporate-cash:v1"
_SCHEMA = 1


def _event_id(prefix: str, event_key: str) -> str:
    return prefix + ":" + hashlib.sha256(event_key.encode("utf-8")).hexdigest()


def _keys(payload: dict, expected: set[str]) -> None:
    if type(payload) is not dict or set(payload) != expected:
        raise CorporateActionError("unsupported or malformed corporate-action journal record")


class CorporateSession:
    """One independent book with append-before-ack and deterministic recovery.

    Each submission computes the next state *before* appending the intent.
    The journal commit precedes in-memory update. Recovery repeats all state
    transitions and verifies their exact digests, not merely JSON syntax.
    A complete native Portfolio/Cache/Account adjustment is NOT implemented.
    """

    def __init__(self, journal: EventJournal, initial_book: CorporateBook) -> None:
        if type(journal) is not EventJournal or type(initial_book) is not CorporateBook:
            raise CorporateActionError("explicit original event journal and initial book required")
        if initial_book.action_id is not None:
            raise CorporateActionError("new journal must start before action recognition, not from an already retired book")
        self.journal = journal
        self.initial_book = initial_book
        journal.verify()
        events = journal.read_all()
        identity = {"schema": _SCHEMA, "book": initial_book.record()}
        if not events:
            journal.append(_SESSION, "session", initial_book.last_observed_ns, identity)
        else:
            first = events[0]
            if (
                first["event_id"] != _SESSION
                or first["kind"] != "session"
                or first["timestamp_ns"] != initial_book.last_observed_ns
                or first["payload"] != identity
            ):
                raise CorporateActionError("corporate-action session identity/account assumptions differ")
        self.book = self.replay()

    def replay(self) -> CorporateBook:
        self.journal.verify()
        events = self.journal.read_all()
        if not events or events[0]["event_id"] != _SESSION:
            raise CorporateActionError("corporate session header missing")
        book = self.initial_book
        for event in events[1:]:
            kind, payload, stamp = event["kind"], event["payload"], event["timestamp_ns"]
            try:
                if kind == "corporate_action":
                    _keys(payload, {"action", "reconciliation", "book_digest"})
                    action = CashTermination.from_record(payload["action"])
                    recon = ReconciledBook.from_record(payload["reconciliation"])
                    if event["event_id"] != _event_id("corp-action", action.action_id):
                        raise CorporateActionError("action event ID does not bind action identity")
                    book = recognize(book, action, recon, observed_ns=stamp)
                elif kind == "corporate_cash":
                    _keys(payload, {"receipt", "book_digest"})
                    receipt = CashReceipt.from_record(payload["receipt"])
                    if event["event_id"] != _event_id("corp-cash", receipt.receipt_id):
                        raise CorporateActionError("cash event ID does not bind transfer identity")
                    book = receive(book, receipt, observed_ns=stamp)
                else:
                    raise CorporateActionError("foreign order/fill/cash or unclassified journal event")
                if payload["book_digest"] != _digest(book.record()):
                    raise CorporateActionError("corporate cashflow replay diverged from committed independent book")
            except (TypeError, ValueError, ArithmeticError, KeyError, OverflowError) as exc:
                raise CorporateActionError(
                    f"corporate journal event {event['sequence']} cannot reconstruct the signed obligation: {exc}"
                ) from exc
        return book

    def recognize(self, action: CashTermination, reconciliation: ReconciledBook, *, observed_ns: int) -> CorporateBook:
        next_book = recognize(self.book, action, reconciliation, observed_ns=observed_ns)
        payload = {
            "action": action.record(),
            "reconciliation": reconciliation.record(),
            "book_digest": _digest(next_book.record()),
        }
        self.journal.append(_event_id("corp-action", action.action_id), "corporate_action", observed_ns, payload)
        self.book = next_book
        return next_book

    def receive(self, receipt: CashReceipt, *, observed_ns: int) -> CorporateBook:
        next_book = receive(self.book, receipt, observed_ns=observed_ns)
        if next_book is self.book:
            return self.book
        payload = {"receipt": receipt.record(), "book_digest": _digest(next_book.record())}
        self.journal.append(_event_id("corp-cash", receipt.receipt_id), "corporate_cash", observed_ns, payload)
        self.book = next_book
        return next_book

    def assert_recovered(self) -> None:
        """Ensure no divergence between current in-memory state and durable log."""
        if self.replay() != self.book:
            raise JournalError("corporate book disagrees with event-journal replay")
