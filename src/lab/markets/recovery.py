"""Deterministic, fail-closed recovery of an OFFLINE reference simulation.

Never reconstruct broker orders or external account balances here. Re-execute
durable source inputs through the *same* simulator and demand byte-equivalent
normalized evidence for every derived fill, reject and funding event. A truncated
or mismatched journal is refused rather than silently inventing an account state.
"""

from __future__ import annotations

import json
from decimal import Decimal

from lab.markets.domain import Instrument, MarkPrice, OrderIntent, Side, Trade
from lab.markets.journal import EventJournal, JournalError
from lab.markets.simulator import OfflineSimulator, SimulationLimits


class _ReplayCollector:
    """Deliberately unconnected sink: journal API surface, no order/network I/O."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def read_all(self) -> tuple:
        return ()

    def append(self, event_id: str, kind: str, timestamp_ns: int, payload: dict) -> int:
        self.events.append({"event_id": event_id, "kind": kind, "timestamp_ns": timestamp_ns, "payload": payload})
        return len(self.events)


def _evidence(record: dict) -> tuple:
    """Decimal is stored in strings. Compare normalized payloads, never float."""
    return (
        record["event_id"],
        record["kind"],
        record["timestamp_ns"],
        json.dumps(record["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


def recover_offline(
    instrument: Instrument,
    journal: EventJournal,
    *,
    starting_balance: Decimal = Decimal("1000"),
    limits: SimulationLimits | None = None,
    taker_fee_bps: Decimal = Decimal("5"),
    slippage_bps: Decimal = Decimal("2"),
) -> OfflineSimulator:
    """Verify every durable event and resume *only* a matching local offline run.

    All replay inputs must be captured in the journal. Unknown order lifecycle
    events, mismatching account assumptions or incomplete derived fills reject
    recovery. For a broker, an independently reconciled execution store is
    mandatory instead; this helper has no authenticated execution capability.
    """
    journal.verify()
    stored = journal.read_all()
    if not stored:
        raise JournalError("recovery requires a nonempty session journal")
    sink = _ReplayCollector()
    sim = OfflineSimulator(
        instrument,
        starting_balance=starting_balance,
        limits=limits,
        taker_fee_bps=taker_fee_bps,
        slippage_bps=slippage_bps,
        journal=sink,
    )
    if _evidence(stored[0]) != _evidence(sink.events[0]):
        raise JournalError("offline session identity mismatch; risk, contract or capital changed")

    for record in stored[1:]:
        kind, event_id, ts, payload = (
            record["kind"],
            record["event_id"],
            record["timestamp_ns"],
            record["payload"],
        )
        try:
            if kind == "mark":
                if not event_id.startswith("valuation:"):
                    raise JournalError("unexpected mark event identifier")
                sim.on_mark(
                    MarkPrice(
                        event_id.removeprefix("valuation:"),
                        ts,
                        int(payload["received_ns"]),
                        Decimal(payload["mark_price"]),
                    )
                )
            elif kind == "trade":
                if not event_id.startswith("trade:"):
                    raise JournalError("unexpected trade event identifier")
                sim.on_trade(
                    Trade(
                        event_id.removeprefix("trade:"),
                        ts,
                        int(payload["received_ns"]),
                        Decimal(payload["price"]),
                        Decimal(payload["quantity"]),
                    )
                )
            elif kind == "intent":
                if not event_id.startswith("intent:"):
                    raise JournalError("unexpected order intent identifier")
                sim.submit(
                    OrderIntent(
                        event_id.removeprefix("intent:"),
                        payload["instrument"],
                        Side(payload["side"]),
                        Decimal(payload["quantity"]),
                        ts,
                        reduce_only=payload["reduce_only"],
                    )
                )
            elif kind == "cancel":
                order_id = event_id.removeprefix("cancel:")
                if not event_id.startswith("cancel:") or order_id != payload["order_id"]:
                    raise JournalError("invalid cancellation identity")
                if not sim.cancel(order_id):
                    raise JournalError("cancel refers to an unknown or already-closed order")
            elif kind == "funding":
                if not event_id.startswith("funding:"):
                    raise JournalError("unexpected funding identity")
                sim.apply_funding(event_id.removeprefix("funding:"), Decimal(payload["rate"]), ts)
            elif kind not in {"fill", "reject"}:
                raise JournalError(f"unsupported offline event kind: {kind}")
            # fill/reject are generated by the simulator from source events.
            # Never apply them a second time as free-standing ledger mutations.
        except (TypeError, ValueError, KeyError, ArithmeticError, OverflowError) as exc:
            raise JournalError(f"offline journal replay rejected event {event_id}: {exc}") from exc

    if len(sink.events) != len(stored):
        raise JournalError("offline journal is incomplete or contains unexplained derived events")
    for index, (expected, observed) in enumerate(zip(stored, sink.events, strict=True), 1):
        if _evidence(expected) != _evidence(observed):
            raise JournalError(f"offline journal event {index} differs from deterministic replay")
    journal.verify()
    sim.journal = journal
    return sim
