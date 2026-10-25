"""Broker-neutral multi-market design and offline reference simulation.

D1 exports intentionally exclude any network-capable execution adapter.
"""

from lab.markets.domain import AssetClass, ContractKind, Instrument, OrderIntent, Side, Trade, TradingMode
from lab.markets.minute import MinuteBar, MinuteBuilder
from lab.markets.simulator import OfflineSimulator, SimulationLimits

__all__ = [
    "AssetClass",
    "ContractKind",
    "Instrument",
    "MinuteBar",
    "MinuteBuilder",
    "OfflineSimulator",
    "OrderIntent",
    "Side",
    "SimulationLimits",
    "Trade",
    "TradingMode",
]
