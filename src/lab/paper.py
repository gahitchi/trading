"""
Paper trading: the same agents as in backtests, connected to an Interactive Brokers
PAPER account through NautilusTrader's live node.

Daily cycle (New York time):
  16:10  SignalAgents request the completed daily bars from IB and publish targets
  16:15  ExecutionAgent sizes positions and sends market-on-open orders
  09:30  IB fills them in the opening auction

Requirements on your machine: IB Gateway or TWS running and logged in to your paper
account, with API access enabled (Configure > API > Settings).
"""

from __future__ import annotations

from datetime import date

from nautilus_trader.adapters.interactive_brokers import (
    InteractiveBrokersDataClientConfig, InteractiveBrokersDataClientFactory,
    InteractiveBrokersExecutionClientConfig, InteractiveBrokersExecutionClientFactory,
    InteractiveBrokersInstrumentProviderConfig, MarketDataType, SymbologyMethod,
)
from nautilus_trader.common import Environment
from nautilus_trader.live import LiveNode
from nautilus_trader.model import ActorId, TraderId, Venue

from lab.agents import (
    AllocatorAgent, AllocatorAgentConfig, ExecutionAgent, ExecutionAgentConfig, RecorderAgent,
    RecorderAgentConfig, RiskAgent, RiskAgentConfig, SignalAgent, SignalAgentConfig,
)
from lab.config import LabConfig
from lab.safety import check_paper_settings
from lab.signals.registry import SignalSpec
from lab.universe import instrument_id

IB = "IB"


def build_node(cfg: LabConfig, specs: list[SignalSpec], tickers: list[str]) -> LiveNode:
    p = cfg.paper
    check_paper_settings(p.port, p.account_id)
    if not specs or not tickers:
        raise ValueError("need at least one signal and one ticker")
    unknown = [t for t in tickers if t not in cfg.universe]
    if unknown:
        raise ValueError(f"{unknown} not in [universe] of lab.toml (their listing venue is needed)")

    iids = {t: instrument_id(t, cfg.universe[t]) for t in tickers}
    provider = InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.SIMPLIFIED,
        load_ids=set(iids.values()),
        convert_exchange_to_mic_venue=True,  # IB "NASDAQ" -> XNAS etc., matching our instrument IDs
    )
    data_cfg = InteractiveBrokersDataClientConfig(
        host=p.host, port=p.port, client_id=p.client_id,
        market_data_type=getattr(MarketDataType, p.market_data),
        use_regular_trading_hours=True, instrument_provider=provider,
    )
    exec_cfg = InteractiveBrokersExecutionClientConfig(
        host=p.host, port=p.port, client_id=p.client_id, account_id=p.account_id,
        instrument_provider=provider,
    )
    node = (LiveNode.builder("trading-lab", TraderId("LAB-001"), Environment.LIVE)
            .add_data_client(IB, InteractiveBrokersDataClientFactory(), data_cfg)
            .add_exec_client(IB, InteractiveBrokersExecutionClientFactory(), exec_cfg)
            .with_timeout_connection(30)
            .with_reconciliation(True)
            .build())

    weight = 1.0 / (len(specs) * len(tickers))
    sleeves = {}
    k = 0
    for spec in specs:
        for t in tickers:
            sig = spec.make()
            sleeve = f"{sig.name}:{t}"
            sleeves[sleeve] = (t, weight)
            node.add_actor(SignalAgent(SignalAgentConfig(
                actor_id=ActorId(f"Signal-{k:03d}-{t}"), signal=sig, sleeve=sleeve, ticker=t,
                instrument_id=iids[t], live=True, signal_time=p.signal_time)))
            k += 1
    node.add_actor(AllocatorAgent(AllocatorAgentConfig(actor_id=ActorId("Allocator"), sleeves=sleeves)))
    node.add_actor(RiskAgent(RiskAgentConfig(
        actor_id=ActorId("Risk"), max_weight=cfg.risk.max_weight, max_gross=cfg.risk.max_gross,
        max_drawdown=cfg.risk.max_drawdown)))
    journal_dir = cfg.results_dir / "paper"
    node.add_actor(RecorderAgent(RecorderAgentConfig(
        actor_id=ActorId("Recorder"), journal=str(journal_dir / f"journal-{date.today():%Y%m}.jsonl"))))
    node.add_strategy(ExecutionAgent(ExecutionAgentConfig(
        order_id_tag="EXEC", instruments=iids, venue=Venue(IB), min_trade_value=cfg.costs.min_trade_value,
        rebalance_band=cfg.costs.rebalance_band, cash_buffer=cfg.costs.cash_buffer, capital=p.capital,
        rebalance_time=p.rebalance_time, live=True, paper_account=p.account_id,
        state_path=str(journal_dir / "ledger.json"))))
    return node
