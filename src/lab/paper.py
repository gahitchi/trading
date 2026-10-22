"""
Paper trading: the same agents as in backtests, connected to an Interactive Brokers
PAPER account through NautilusTrader's live node.

Daily cycle (New York time):
  16:10  SignalAgents request the completed daily bars from IB and publish targets
  16:15  ExecutionAgent reconciles its ledger with IB, then sends market-on-open orders
  09:30  IB fills them in the opening auction

Runtime files (results/paper/): ledger.json, heartbeat.json, KILL (kill switch),
journal-YYYYMM.jsonl, smoke-*.json
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from nautilus_trader.adapters.interactive_brokers import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersDataClientFactory,
    InteractiveBrokersExecutionClientConfig,
    InteractiveBrokersExecutionClientFactory,
    InteractiveBrokersInstrumentProviderConfig,
    MarketDataType,
    SymbologyMethod,
)
from nautilus_trader.common import Environment
from nautilus_trader.live import LiveNode, LiveNodeBuilder, LiveRiskEngineConfig
from nautilus_trader.model import ActorId, TraderId, Venue

from lab.agents import (
    AllocatorAgent,
    AllocatorAgentConfig,
    ExecutionAgent,
    ExecutionAgentConfig,
    RecorderAgent,
    RecorderAgentConfig,
    RiskAgent,
    RiskAgentConfig,
    SignalAgent,
    SignalAgentConfig,
)
from lab.agents.alerts import AlertAgent, AlertAgentConfig
from lab.agents.cross_sectional import CrossSectionalAgent, CrossSectionalAgentConfig
from lab.agents.smoke import SmokeTest, SmokeTestConfig
from lab.config import LabConfig
from lab.notify import Notifier
from lab.safety import check_paper_settings
from lab.signals.registry import SignalSpec
from lab.universe import instrument_id

IB = "IB"


def runtime_dir(cfg: LabConfig) -> Path:
    return cfg.results_dir / "paper"


def kill_path(cfg: LabConfig) -> Path:
    return runtime_dir(cfg) / "KILL"


def heartbeat_path(cfg: LabConfig) -> Path:
    return runtime_dir(cfg) / "heartbeat.json"


@dataclass(frozen=True)
class Schedule:
    """New York clock times of the daily cycle (agents/schedule.py). lab paper uses lab.toml's times;
    the live-node harness compresses a session into seconds."""

    signal_time: str
    rebalance_time: str
    session_close: str = "16:00"
    weekdays_only: bool = True
    xs_eval_delay_s: float = 120.0

    @classmethod
    def from_config(cls, cfg: LabConfig) -> Schedule:
        return cls(cfg.paper.signal_time, cfg.paper.rebalance_time)


# Adds the broker's data and execution clients to a node builder. Default: Interactive Brokers.
Clients = Callable[[LabConfig, dict], LiveNodeBuilder]


def _base_node(cfg: LabConfig, tickers: list[str], clients: Clients | None = None):
    p = cfg.paper
    check_paper_settings(p.port, p.account_id)
    unknown = [t for t in tickers if t not in cfg.universe]
    if not tickers or unknown:
        raise ValueError(f"tickers must be in [universe] of lab.toml (unknown: {unknown})")
    iids = {t: instrument_id(t, cfg.universe[t]) for t in tickers}
    builder = (clients or _ib_clients)(cfg, iids)
    node = (
        builder.with_risk_engine_config(LiveRiskEngineConfig(max_order_submit_rate=p.order_rate))
        .with_reconciliation(True)
        .build()
    )
    return node, iids


def _ib_clients(cfg: LabConfig, iids: dict) -> LiveNodeBuilder:
    p = cfg.paper
    provider = InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.SIMPLIFIED,
        load_ids=set(iids.values()),
        convert_exchange_to_mic_venue=True,  # IB "NASDAQ" -> XNAS etc., matching our instrument IDs
    )
    data_cfg = InteractiveBrokersDataClientConfig(
        host=p.host,
        port=p.port,
        client_id=p.client_id,
        market_data_type=getattr(MarketDataType, p.market_data),
        use_regular_trading_hours=True,
        instrument_provider=provider,
    )
    exec_cfg = InteractiveBrokersExecutionClientConfig(
        host=p.host,
        port=p.port,
        client_id=p.client_id,
        account_id=p.account_id,
        instrument_provider=provider,
    )
    return (
        LiveNode.builder("trading-lab", TraderId("LAB-001"), Environment.LIVE)
        .add_data_client(IB, InteractiveBrokersDataClientFactory(), data_cfg)
        .add_exec_client(IB, InteractiveBrokersExecutionClientFactory(), exec_cfg)
        .with_timeout_connection(30)
    )


def build_node(
    cfg: LabConfig,
    specs: list[SignalSpec],
    tickers: list[str],
    *,
    adopt_broker: bool = False,
    notifier: Notifier | None = None,
    clients: Clients | None = None,
    schedule: Schedule | None = None,
) -> LiveNode:
    if not specs:
        raise ValueError("need at least one signal")
    node, iids = _base_node(cfg, tickers, clients)
    p = cfg.paper
    sc = schedule or Schedule.from_config(cfg)
    rt = runtime_dir(cfg)

    per_strategy = 1.0 / len(specs)
    sleeves, absolute = {}, set()
    k = 0
    for spec in specs:
        sig = spec.make()
        if getattr(sig, "cross_sectional", False):
            absolute.add(sig.name)
            node.add_actor(
                CrossSectionalAgent(
                    CrossSectionalAgentConfig(
                        actor_id=ActorId(f"CrossSectional-{sig.name}"),
                        signal=sig,
                        instruments=iids,
                        live=True,
                        signal_time=sc.signal_time,
                        session_close=sc.session_close,
                        weekdays_only=sc.weekdays_only,
                        eval_delay_s=sc.xs_eval_delay_s,
                    )
                )
            )
            for t in tickers:
                sleeves[f"{sig.name}:{t}"] = (t, per_strategy / len(tickers))
            continue
        for t in tickers:
            sig = spec.make()
            sleeve = f"{sig.name}:{t}"
            sleeves[sleeve] = (t, per_strategy / len(tickers))
            node.add_actor(
                SignalAgent(
                    SignalAgentConfig(
                        actor_id=ActorId(f"Signal-{k:03d}-{t}"),
                        signal=sig,
                        sleeve=sleeve,
                        ticker=t,
                        instrument_id=iids[t],
                        live=True,
                        signal_time=sc.signal_time,
                        session_close=sc.session_close,
                        weekdays_only=sc.weekdays_only,
                    )
                )
            )
            k += 1
    node.add_actor(
        AllocatorAgent(
            AllocatorAgentConfig(
                actor_id=ActorId("Allocator"),
                sleeves=sleeves,
                absolute_groups=absolute,
                target_vol=cfg.portfolio.target_vol,
                vol_window=cfg.portfolio.vol_window,
            )
        )
    )
    node.add_actor(
        RiskAgent(
            RiskAgentConfig(
                actor_id=ActorId("Risk"),
                max_weight=cfg.risk.max_weight,
                max_gross=cfg.risk.max_gross,
                max_drawdown=cfg.risk.max_drawdown,
            )
        )
    )
    node.add_actor(
        RecorderAgent(
            RecorderAgentConfig(actor_id=ActorId("Recorder"), journal=str(rt / f"journal-{date.today():%Y%m}.jsonl"))
        )
    )
    node.add_actor(AlertAgent(AlertAgentConfig(actor_id=ActorId("Alerts"), notifier=notifier or Notifier())))
    node.add_strategy(
        ExecutionAgent(
            ExecutionAgentConfig(
                order_id_tag="EXEC",
                instruments=iids,
                venue=Venue(IB),
                min_trade_value=cfg.costs.min_trade_value,
                rebalance_band=cfg.costs.rebalance_band,
                cash_buffer=cfg.costs.cash_buffer,
                capital=p.capital,
                rebalance_time=sc.rebalance_time,
                weekdays_only=sc.weekdays_only,
                max_order_value=p.max_order_value,
                max_orders_per_day=p.max_orders_per_day,
                max_daily_turnover=p.max_daily_turnover,
                live=True,
                paper_account=p.account_id,
                state_path=str(rt / "ledger.json"),
                kill_path=str(kill_path(cfg)),
                heartbeat_path=str(heartbeat_path(cfg)),
                adopt_broker_positions=adopt_broker,
            )
        )
    )
    return node


def build_smoke_node(
    cfg: LabConfig, tickers: list[str], *, test_order: bool = False, clients: Clients | None = None
) -> tuple[LiveNode, Path]:
    node, iids = _base_node(cfg, tickers, clients)
    report = runtime_dir(cfg) / f"smoke-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    node.add_strategy(
        SmokeTest(
            SmokeTestConfig(
                order_id_tag="SMOKE",
                instruments=iids,
                venue=Venue(IB),
                paper_account=cfg.paper.account_id,
                report_path=str(report),
                test_order=test_order,
            )
        )
    )
    return node, report
