"""Portable, pure quantity safety oracle for future broker-bound reduction checks.

This intentionally does NOT submit orders or authorize account actions. Inputs
must come from a trusted broker reconciler. It is independent of NautilusTrader
so its arithmetic invariants can be checked on an Android/Termux Python runtime.
"""

from __future__ import annotations

from decimal import Decimal


class UnreconciledReduction(ValueError):
    """No automated reduction is authorized by the supplied evidence."""


def _quantity(value: Decimal | int | str, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise UnreconciledReduction(f"{name}: exact Decimal, integer or decimal string required")
    try:
        item = Decimal(value)
    except (ValueError, ArithmeticError) as exc:
        raise UnreconciledReduction(f"{name}: invalid decimal quantity") from exc
    if not item.is_finite() or (positive and item <= 0):
        raise UnreconciledReduction(f"{name}: finite {'positive' if positive else 'numeric'} quantity required")
    return item


def capped_reduction(
    *,
    broker_position: Decimal | int | str,
    requested_change: Decimal | int | str,
    lot_step: Decimal | int | str,
    unsettled_reduction: Decimal | int | str,
    broker_position_reconciled: bool,
    broker_orders_reconciled: bool,
    instrument_definition_reconciled: bool,
    pending_increase_absent: bool,
    expected_action_version: str,
    broker_action_version: str,
) -> Decimal:
    """Bound a *hypothetical* reduce-only order without accidental reversal.

    Signed values: positive position is long; negative is short; requested
    change is negative to SELL and positive to BUY. Unsettled reduction is a
    conservative absolute quantity: even a pending cancel can still fill.
    All open orders (including partially filled and unknown statuses) must be
    reflected in the broker state before invoking this reference oracle.

    Truncate rather than round to the current instrument lot step. A split,
    reverse split or GTC order adjustment changes the instrument action
    version; no automatic reduction is sized until *both* versions match.

    This does not establish instrument/account authenticity, place orders,
    qualify market data, authorize brokerage execution, or perform an atomic
    check against a live OMS. Integration needs broker-scoped state leases.
    """
    if not all(
        type(flag) is bool and flag
        for flag in (
            broker_position_reconciled,
            broker_orders_reconciled,
            instrument_definition_reconciled,
            pending_increase_absent,
        )
    ):
        raise UnreconciledReduction("broker position/order/instrument state is not fully reconciled")
    if (
        not isinstance(expected_action_version, str)
        or not expected_action_version
        or not isinstance(broker_action_version, str)
        or broker_action_version != expected_action_version
    ):
        raise UnreconciledReduction("corporate-action version is unconfirmed; reconcile positions and GTC orders")

    held = _quantity(broker_position, "broker_position")
    delta = _quantity(requested_change, "requested_change")
    step = _quantity(lot_step, "lot_step", positive=True)
    pending = _quantity(unsettled_reduction, "unsettled_reduction")
    if pending < 0:
        raise UnreconciledReduction("unsettled reduction cannot be negative")
    if held == 0:
        if pending or delta:
            raise UnreconciledReduction("zero position with unresolved requested/pending change")
        return Decimal(0)
    if delta * held > 0:
        raise UnreconciledReduction("requested change increases exposure")
    if pending > abs(held):
        raise UnreconciledReduction("pending reductions exceed reconciled inventory")
    available = min(abs(delta), abs(held) - pending)
    if not available:
        return Decimal(0)
    # Exact positive-quantity truncation: nearest-lot rounding could flip.
    size = (available // step) * step
    return -size if held > 0 else size
