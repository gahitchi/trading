"""Broker-neutral multi-market design and offline reference simulation.

D1 exports intentionally exclude any network-capable execution adapter.
"""

from lab.markets.domain import AssetClass, ContractKind, Instrument, MarkPrice, OrderIntent, Side, Trade, TradingMode
from lab.markets.minute import MinuteBar, MinuteBuilder
from lab.markets.simulator import OfflineSimulator, SimulationLimits

__all__ = [
    "AssetClass",
    "ContractKind",
    "Instrument",
    "MarkPrice",
    "MinuteBar",
    "MinuteBuilder",
    "OfflineSimulator",
    "OrderIntent",
    "Side",
    "SimulationLimits",
    "Trade",
    "TradingCadence",
    "TradingMode",
    "aggregate_trades",
]


def __getattr__(name: str):
    """Load NumPy-backed historical aggregation only on explicit request.

    Normal typed offline simulator/domain imports must remain standard-library
    compatible (including in native Termux). Historical arrays keep their
    existing NumPy implementation, not a diluted phone-only substitute.
    """
    if name in {"TradingCadence", "aggregate_trades"}:
        from lab.markets.minute_batch import TradingCadence, aggregate_trades

        return {"TradingCadence": TradingCadence, "aggregate_trades": aggregate_trades}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
