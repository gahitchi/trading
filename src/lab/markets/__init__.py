"""Broker-neutral multi-market design and offline reference simulation.

D1 exports intentionally exclude any network-capable execution adapter.
"""

from lab.markets.domain import AssetClass, ContractKind, Instrument, MarkPrice, OrderIntent, Side, Trade, TradingMode
from lab.markets.minute import MinuteBar, MinuteBuilder
from lab.markets.minute_batch import TradingCadence, aggregate_trades
from lab.markets.simulator import OfflineSimulator, SimulationLimits

__all__ = [
    "AssetClass",
    "ContractKind",
    "Instrument",
    "MarkPrice",
    "MinuteBar",
    "MinuteBuilder",
    "TradingCadence",
    "aggregate_trades",
    "OfflineSimulator",
    "OrderIntent",
    "Side",
    "SimulationLimits",
    "Trade",
    "TradingMode",
]
