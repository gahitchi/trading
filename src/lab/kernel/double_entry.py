"""Deterministic double-entry projector independent of Nautilus, brokers and OMS.

Signed debits are positive; credits are negative. Positions carry historical
signed acquisition cost, corporate claims are signed receivables/payables,
earnings are a credit balance (the negative of realized PnL). No valuation,
fills, execution routing, market data or crypto exchange models exist here.

This module validates the *financial* projection of known domain transitions.
It cannot infer whether a reported broker receipt or settled instrument is real.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class AccountingInvariantError(ValueError):
    """The double-entry state is invalid or cannot be reconstructed."""


class Account(StrEnum):
    CASH = "cash"
    SECURITY_BASIS = "security_basis"
    CORPORATE_CLAIM = "corporate_claim"
    REALIZED_EARNINGS = "realized_earnings"


_ACCOUNTS = tuple(Account)
_VERSION = 1
_KINDS = {"corporate_action", "corporate_cash"}


def _money(value: Decimal, quantum: Decimal, label: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise AccountingInvariantError(f"{label} requires exact finite Decimal")
    if value % quantum:
        raise AccountingInvariantError(f"{label} exceeds settlement-currency quantum")
    return value


def _quantum(q: Decimal) -> Decimal:
    if type(q) is not Decimal or not q.is_finite() or q <= 0:
        raise AccountingInvariantError("invalid positive exact settlement quantum")
    if q != Decimal(1).scaleb(q.as_tuple().exponent):
        raise AccountingInvariantError("currency quantum must be a power of ten")
    return q


def _hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class AccountingState:
    """Explicit signed normal balances, not a mark-to-market position valuation."""

    currency: str
    quantum: Decimal
    cash: Decimal
    security_basis: Decimal
    corporate_claim: Decimal
    realized_earnings: Decimal

    def __post_init__(self) -> None:
        if type(self.currency) is not str or not 3 <= len(self.currency) <= 8 or not self.currency.isalpha() or not self.currency.isupper():
            raise AccountingInvariantError("invalid accounting settlement currency")
        q = _quantum(self.quantum)
        for field in _ACCOUNTS:
            _money(getattr(self, field.value), q, field.value)

    def accounts(self) -> tuple[tuple[Account, Decimal], ...]:
        return tuple((account, getattr(self, account.value)) for account in _ACCOUNTS)

    def record(self) -> dict:
        return {
            "currency": self.currency,
            "quantum": str(self.quantum),
            **{a.value: str(v) for a, v in self.accounts()},
        }

    @property
    def owner_capital(self) -> Decimal:
        """Balancing opening capital: capital is a CREDIT (negative)."""
        return -sum((amount for _, amount in self.accounts()), Decimal(0))

    @property
    def fingerprint(self) -> str:
        return _hash(self.record())


@dataclass(frozen=True, slots=True)
class Posting:
    account: Account
    debit: Decimal

    def __post_init__(self) -> None:
        if type(self.account) is not Account or type(self.debit) is not Decimal or not self.debit.is_finite():
            raise AccountingInvariantError("posting requires a known account and exact finite Decimal")
        if not self.debit:
            raise AccountingInvariantError("zero entries must be omitted rather than forged")

    def record(self) -> dict:
        return {"account": self.account.value, "debit": str(self.debit)}


@dataclass(frozen=True, slots=True)
class PostingBatch:
    kind: str
    currency: str
    entries: tuple[Posting, ...]
    closing_state_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise AccountingInvariantError("unsupported financial event type")
        if not isinstance(self.entries, tuple):
            raise AccountingInvariantError("postings must be immutable")
        if any(type(entry) is not Posting for entry in self.entries):
            raise AccountingInvariantError("invalid journal posting type")
        if tuple(entry.account for entry in self.entries) != tuple(
            account for account in _ACCOUNTS if any(entry.account is account for entry in self.entries)
        ):
            raise AccountingInvariantError("postings must be unique in canonical account order")
        if sum((entry.debit for entry in self.entries), Decimal(0)) != 0:
            raise AccountingInvariantError("unbalanced postings cannot enter accounting journal")
        if type(self.closing_state_sha256) is not str or len(self.closing_state_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.closing_state_sha256
        ):
            raise AccountingInvariantError("invalid closing financial-state digest")

    def record(self) -> dict:
        return {
            "version": _VERSION,
            "kind": self.kind,
            "currency": self.currency,
            "entries": [entry.record() for entry in self.entries],
            "closing_state_sha256": self.closing_state_sha256,
        }


def transition(before: AccountingState, after: AccountingState, *, kind: str) -> PostingBatch:
    """Independent financial conservation proof for a domain event.

    The sole unchanged opening-capital invariant prevents quietly injecting
    cash, inventing profits or deleting a corporate payable without balancing
    entries. Domain-specific event validation is performed by the domain
    reducer BEFORE constructing this proof.
    """
    if type(before) is not AccountingState or type(after) is not AccountingState:
        raise AccountingInvariantError("typed before/after financial states required")
    if before.currency != after.currency or before.quantum != after.quantum:
        raise AccountingInvariantError("multi-currency conversion is not implicit")
    if before.owner_capital != after.owner_capital:
        raise AccountingInvariantError("economic conservation failure: opening capital changed")
    diffs = {a: getattr(after, a.value) - getattr(before, a.value) for a in _ACCOUNTS}
    if kind == "corporate_action" and diffs[Account.CASH] != 0:
        raise AccountingInvariantError("corporate action recognition must not move actual cash")
    if kind == "corporate_cash" and (
        diffs[Account.SECURITY_BASIS] != 0 or diffs[Account.REALIZED_EARNINGS] != 0
        or diffs[Account.CASH] != -diffs[Account.CORPORATE_CLAIM]
    ):
        raise AccountingInvariantError("corporate receipt changes only cash and outstanding claim")
    entries = tuple(Posting(a, diffs[a]) for a in _ACCOUNTS if diffs[a] != 0)
    for entry in entries:
        _money(entry.debit, before.quantum, entry.account.value)
    return PostingBatch(kind=kind, currency=before.currency, entries=entries, closing_state_sha256=after.fingerprint)


def verify_record(
    record: dict, before: AccountingState, after: AccountingState, *, kind: str
) -> PostingBatch:
    """Reconstruct the entire expected batch, including account order and hash.

    This is stronger than accepting a caller's claim that entries summed to
    zero: a zero-sum forgery against different accounts must also be rejected.
    """
    expected = transition(before, after, kind=kind)
    if type(record) is not dict or record != expected.record():
        raise AccountingInvariantError("durable double-entry postings do not match financial state transition")
    return expected
