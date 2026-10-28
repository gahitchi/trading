"""Measured F1 replay/append workload for independent operator evidence.

Deterministic generated inputs; timings are local measurements, not source
authentication, a production throughput guarantee, or F1 qualification.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from lab.economic_store import EconomicEventStore
from lab.engine_events import FillFact

MAX_MEASUREMENT_EVENTS = 10_000


@dataclass(frozen=True, slots=True)
class F1ReplayMeasurement:
    event_count: int
    append_ms: float
    cold_replay_ms: float
    economic_head: str
    selected_limits_passed: bool | None
    declared_machine_verified: bool = False
    broker_source_authenticated: bool = False
    f1_qualified: bool = False

    def to_document(self) -> dict:
        return {
            "schema_version": 1,
            "purpose": "F1_LOCAL_REPLAY_PERFORMANCE_UNQUALIFIED",
            "event_count": self.event_count,
            "append_ms": self.append_ms,
            "cold_replay_ms": self.cold_replay_ms,
            "economic_head": self.economic_head,
            "selected_limits_passed": self.selected_limits_passed,
            "declared_machine_verified": self.declared_machine_verified,
            "broker_source_authenticated": self.broker_source_authenticated,
            "f1_qualified": self.f1_qualified,
        }


def measure_f1_replay(
    event_count: int,
    *,
    max_append_ms: float | None = None,
    max_cold_replay_ms: float | None = None,
) -> F1ReplayMeasurement:
    """Append, cold-restart and byte-compare the same deterministic receipt set."""
    if type(event_count) is not int or not 0 < event_count <= MAX_MEASUREMENT_EVENTS:
        raise ValueError("F1 benchmark event_count must be a bounded positive integer")
    if (max_append_ms is None) != (max_cold_replay_ms is None):
        raise ValueError("both operator-selected limits must be supplied together")
    if max_append_ms is not None and (
        type(max_append_ms) not in (float, int)
        or type(max_cold_replay_ms) not in (float, int)
        or not 0 < max_append_ms < 1_000_000_000
        or not 0 < max_cold_replay_ms < 1_000_000_000
    ):
        raise ValueError("performance acceptance limits must be bounded positive finite milliseconds")
    with tempfile.TemporaryDirectory(prefix="f1-replay-evidence-") as scratch:
        path = Path(scratch) / "original-economics.db"
        store = EconomicEventStore(path, "BENCH-USD")
        book = store.load()
        append_start = time.perf_counter_ns()
        for index in range(1, event_count + 1):
            book = store.append(
                FillFact(
                    event_id=f"benchmark-fill-{index:08d}",
                    source_ref=f"synthetic:benchmark:{index:08d}",
                    timestamp_ns=index,
                    symbol="SPY",
                    side="BUY" if index % 2 else "SELL",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    commission=Decimal("0"),
                ),
                expected_head=book.evidence_head,
            )
        append_ms = (time.perf_counter_ns() - append_start) / 1_000_000
        del store
        cold_start = time.perf_counter_ns()
        restored = EconomicEventStore(path, "BENCH-USD", readonly=True).load()
        replay_ms = (time.perf_counter_ns() - cold_start) / 1_000_000
        if restored != book or len(restored.events) != event_count:
            raise ValueError("F1 measured replay differs from its deterministic economic source")
        limits_passed = (
            append_ms <= max_append_ms and replay_ms <= max_cold_replay_ms
            if max_append_ms is not None
            else None
        )
        return F1ReplayMeasurement(
            event_count=event_count,
            append_ms=append_ms,
            cold_replay_ms=replay_ms,
            economic_head=book.evidence_head,
            selected_limits_passed=limits_passed,
        )


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unqualified, synthetic F1 replay performance measurement")
    parser.add_argument("--events", type=int, default=64)
    parser.add_argument("--max-append-ms", type=float)
    parser.add_argument("--max-cold-replay-ms", type=float)
    args = parser.parse_args(argv)
    try:
        measurement = measure_f1_replay(
            args.events, max_append_ms=args.max_append_ms, max_cold_replay_ms=args.max_cold_replay_ms
        )
    except (OSError, ValueError) as exc:
        print(f"F1 benchmark refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(measurement.to_document(), sort_keys=True, separators=(",", ":")))
    return 2 if measurement.selected_limits_passed is False else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
