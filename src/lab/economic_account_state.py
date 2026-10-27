"""F1.2 account-state evidence checks; read-only, USD cash accounts only.

A native AccountState is independent of our journal business events, but is
NOT broker attestation, an atomic snapshot, or an executable settlement path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lab.economic_ledger import EconomicLedger
from lab.economic_observation import (
    NativeCashObservation,
    NativeFillObservation,
    NativeParity,
    ObservationError,
    _money,
    reconcile_native,
)


@dataclass(frozen=True, slots=True)
class NativeAccountStateEvidence:
    account_id: str
    event_id: str
    ts_event: int
    ts_init: int
    is_reported: bool
    cash: Decimal
    free: Decimal
    locked: Decimal


@dataclass(frozen=True, slots=True)
class NativeAccountStateParity:
    trading: NativeParity
    state_cash_delta: Decimal
    is_reported: bool

    @property
    def matches(self) -> bool:
        """Exact arithmetic only; not a release or broker-provenance assertion."""
        return self.trading.matches and self.state_cash_delta == 0


def observe_account_state(state: object) -> NativeAccountStateEvidence:
    """Extract and verify one native account event, without touching account state.

    AccountBalance.total is a valid F1 cash observation only for a single
    USD-base CASH account with precisely one USD balance and no margin records.
    AccountState.is_reported must be kept: calculated != broker-reported.
    """
    if getattr(getattr(state, "account_type", None), "name", None) != "CASH":
        raise ObservationError("account-state evidence requires a USD cash account")
    if getattr(getattr(state, "base_currency", None), "code", None) != "USD":
        raise ObservationError("account-state evidence requires explicit USD base currency")
    account_id = str(getattr(state, "account_id", ""))
    event_id = str(getattr(state, "event_id", ""))
    if account_id in ("", "None") or event_id in ("", "None") or any(c.isspace() for c in account_id + event_id):
        raise ObservationError("native account event is missing a stable identity")
    ts_event = getattr(state, "ts_event", None)
    ts_init = getattr(state, "ts_init", None)
    if (
        type(ts_event) is not int
        or type(ts_init) is not int
        or ts_event <= 0
        or ts_init < ts_event
    ):
        raise ObservationError("native account timestamps are missing or out of order")
    is_reported = getattr(state, "is_reported", None)
    if type(is_reported) is not bool:
        raise ObservationError("native reported-versus-calculated origin is missing")
    balances = getattr(state, "balances", None)
    margins = getattr(state, "margins", None)
    if type(balances) not in (tuple, list) or len(balances) != 1:
        raise ObservationError("native cash state must have exactly one currency balance")
    if type(margins) not in (tuple, list) or margins:
        raise ObservationError("native cash state cannot have margin balances")
    balance = balances[0]
    total = _money(getattr(balance, "total", None), "account total")
    free = _money(getattr(balance, "free", None), "account free")
    locked = _money(getattr(balance, "locked", None), "account locked")
    if locked < 0 or free + locked != total:
        raise ObservationError("native account balance does not conserve exactly")
    return NativeAccountStateEvidence(
        account_id, event_id, ts_event, ts_init, is_reported, total, free, locked
    )


def reconcile_account_state(
    ledger: EconomicLedger,
    snapshot: NativeCashObservation,
    fills: tuple[NativeFillObservation, ...],
    state: NativeAccountStateEvidence,
) -> NativeAccountStateParity:
    """Corroborate snapshot cash against the separately captured native event.

    Identity and event timestamps must be identical. In particular, a stale
    account event cannot be silently used as if it were the current snapshot.
    This does NOT prove the position scope is exhaustive or the observation
    was atomically captured with the fills.
    """
    if type(state) is not NativeAccountStateEvidence:
        raise ObservationError("typed native account-state evidence required")
    if snapshot.account_id != state.account_id or snapshot.as_of_ns != state.ts_event:
        raise ObservationError("account-state identity or observation time differs")
    if type(state.is_reported) is not bool:
        raise ObservationError("account-state origin classification required")
    if state.free + state.locked != state.cash or state.locked < 0:
        raise ObservationError("tampered account-state balance")
    trading = reconcile_native(ledger, snapshot, fills)
    return NativeAccountStateParity(
        trading, snapshot.cash - state.cash, state.is_reported
    )


def validate_account_state_sequence(
    states: tuple[NativeAccountStateEvidence, ...],
) -> tuple[str, ...]:
    """Refuse duplicates, cross-account replay and non-monotonic account events.

    This checks the visible sequence only: absence of an event is unknowable
    without an externally authenticated venue or broker event source.
    """
    if type(states) is not tuple or not states:
        raise ObservationError("nonempty immutable native account-state sequence required")
    account_id = states[0].account_id
    observed: set[str] = set()
    prev_ts = -1
    for state in states:
        if type(state) is not NativeAccountStateEvidence:
            raise ObservationError("typed native account-state sequence required")
        if (
            state.account_id != account_id
            or not account_id
            or state.event_id in observed
            or not state.event_id
            or type(state.ts_event) is not int
            or type(state.ts_init) is not int
            or state.ts_event <= prev_ts
            or state.ts_init < state.ts_event
        ):
            raise ObservationError("duplicate, foreign or non-monotonic native account event")
        observed.add(state.event_id)
        prev_ts = state.ts_event
    return tuple(state.event_id for state in states)
