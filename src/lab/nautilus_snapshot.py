"""Capture NautilusTrader sandbox cache account state into the portable journal.

Read-only, paper/replay only. This module deliberately imports no Nautilus
types: the cache producer is duck-typed, while its real Nautilus integration
is exercised in the full CI suite. An observed cache is NOT an independent
broker/exchange statement or sufficient evidence for risk authorization.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from lab.paper_account_snapshot import PaperAccountSnapshot, PaperAccountSnapshotJournal
from lab.sandbox_capture import PaperOrderEventCapture

_TERMINAL = {"DENIED", "REJECTED", "CANCELED", "EXPIRED", "FILLED"}


def _exact(value, name: str) -> str:
    if value is None or isinstance(value, (bool, float)):
        raise ValueError(f"{name} requires an exact finite decimal")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not decimal") from exc
    if not amount.is_finite():
        raise ValueError(f"{name} is nonfinite")
    return str(amount)


def _balances(account) -> dict[str, dict[str, str]]:
    original = account.balances()
    if not isinstance(original, dict) or not original:
        raise ValueError("sandbox account has no complete currency balances")
    balances = {}
    for currency, balance in original.items():
        ccy = str(getattr(currency, "code", currency))
        if ccy in balances or str(balance.currency.code) != ccy:
            raise ValueError("sandbox cash balance currency mismatch")
        fields = {}
        for target, name in (("total", "total"), ("available", "free"), ("locked", "locked")):
            money = getattr(balance, name)
            if money is None or money.currency.code != ccy:
                raise ValueError("sandbox cash balance has missing or mixed-currency Money")
            # Native Nautilus Money.as_decimal avoids binary float rounding.
            fields[target] = _exact(money.as_decimal(), f"{ccy} {target}")
        balances[ccy] = fields
    return balances


def _active_orders(cache, instrument_id: str) -> dict[str, str]:
    """Include ALL nonterminal orders, including local/inflight/emulated."""
    active = {}
    for order in cache.orders():
        name = str(getattr(getattr(order, "status", None), "name", getattr(order, "status", None)))
        if name in _TERMINAL:
            continue
        if str(order.instrument_id) != instrument_id:
            raise ValueError("sandbox account contains orders outside captured instrument")
        coid = str(order.client_order_id)
        if coid in active:
            raise ValueError("duplicate active client order ID in sandbox cache")
        leaves = _exact(order.leaves_qty, "sandbox order remaining quantity")
        if Decimal(leaves) <= 0:
            raise ValueError("nonterminal sandbox order has no positive leaves quantity")
        active[coid] = leaves
    return active


def verify_fresh_sandbox_start(cache, iid, capture: PaperOrderEventCapture) -> str:
    """Observe a fresh account at strategy start, before any submitted orders.

    A fresh local cache is only a *simulation-scope assumption*: it does not
    prove that any external exchange/broker balance is independent or flat.
    """
    if str(iid) != capture.instrument_id or capture.receipts():
        raise ValueError("snapshot session must start with a fresh order capture")
    account = cache.account_for_venue(iid.venue)
    if account is None:
        raise ValueError("sandbox account is missing or ambiguous for venue")
    if cache.orders() or cache.positions_open():
        raise ValueError("sandbox account did not start flat with zero existing orders")
    _balances(account)
    return str(account.id)


def record_sandbox_snapshot(
    cache,
    iid,
    capture: PaperOrderEventCapture,
    journal: PaperAccountSnapshotJournal,
    *,
    at_ns: int,
    initial_account_id: str,
) -> dict:
    """Inspect an actual Nautilus cache, then persist a causally bound snapshot.

    No defaulting missing cache fields to zero. Only one isolated
    instrument/venue/account is supported; other exposure fails closed.
    """
    if type(at_ns) is not int or at_ns <= 0:
        raise ValueError("sandbox snapshot time must be a positive integer")
    if str(iid) != capture.instrument_id:
        raise ValueError("sandbox capture instrument mismatch")
    account = cache.account_for_venue(iid.venue)
    if account is None or str(account.id) != initial_account_id:
        raise ValueError("sandbox account identity is missing, changed or ambiguous")
    if journal.account_id != initial_account_id:
        raise ValueError("snapshot archive account is not the original sandbox account")

    positions = {}
    signed = Decimal(0)
    for position in cache.positions_open():
        if str(position.instrument_id) != capture.instrument_id:
            raise ValueError("other instrument position in isolated sandbox account")
        signed += Decimal(_exact(position.signed_qty, "sandbox signed position"))
    if signed:
        positions[capture.instrument_id] = str(signed)
    orders = _active_orders(cache, capture.instrument_id)
    receipts = capture.receipts()
    _history, digest = capture.evidence_prefix(len(receipts))

    count = len(journal.snapshots()) + 1
    snapshot = PaperAccountSnapshot(
        snapshot_id=f"{capture.session_id}:snapshot:{count}",
        source_id=f"{capture.session_id}:cache:{count}",
        source_kind="sandbox-cache",
        account_id=initial_account_id,
        captured_ns=at_ns,
        capture_count=len(receipts),
        capture_sha256=digest,
        started_flat=True,
        exclusive_scope=True,
        complete_snapshot=True,
        positions=positions,
        open_orders=orders,
        balances=_balances(account),
    )
    return journal.append(snapshot)
