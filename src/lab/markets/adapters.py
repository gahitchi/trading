"""Execution/data adapter boundaries, with deliberate refusal of premature broker routing.

NautilusTrader client factories will be wired through these contracts only after
per-venue acceptance. This file intentionally has no REST/WebSocket order client.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from lab.markets.domain import Capabilities, Instrument, OrderIntent, Trade, TradingMode, UnsupportedMode
from lab.markets.simulator import Fill, OfflineSimulator, SimulationLimits


@runtime_checkable
class ExecutionAdapter(Protocol):
    @property
    def mode(self) -> TradingMode: ...

    @property
    def capabilities(self) -> Capabilities: ...

    def submit(self, intent: OrderIntent) -> None: ...

    def cancel(self, order_id: str) -> bool: ...


@runtime_checkable
class ReplayDataAdapter(Protocol):
    def replay_trade(self, trade: Trade) -> tuple[Fill, ...]: ...


@dataclass(slots=True)
class OfflineAdapter:
    """First authorized implementation of the cross-market execution interface."""

    simulator: OfflineSimulator

    @property
    def mode(self) -> TradingMode:
        return TradingMode.OFFLINE_SIM

    @property
    def capabilities(self) -> Capabilities:
        return self.simulator.capabilities

    def submit(self, intent: OrderIntent) -> None:
        self.simulator.submit(intent)

    def cancel(self, order_id: str) -> bool:
        return self.simulator.cancel(order_id)

    def replay_trade(self, trade: Trade) -> tuple[Fill, ...]:
        return self.simulator.on_trade(trade)


def open_execution_adapter(
    mode: TradingMode,
    instrument: Instrument,
    *,
    starting_balance: Decimal = Decimal("1000"),
    limits: SimulationLimits | None = None,
) -> OfflineAdapter:
    """Fail closed when asked for a broker demo or live connector before qualification."""
    if mode != TradingMode.OFFLINE_SIM:
        raise UnsupportedMode(
            f"{mode}: no externally routed order implementation is approved; only offline simulation exists"
        )
    return OfflineAdapter(OfflineSimulator(instrument, starting_balance=starting_balance, limits=limits))
