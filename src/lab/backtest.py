"""
Run the full agent system over historical data in NautilusTrader's backtest engine.

One run = one portfolio: a set of sleeves (signal x ticker x weight), all agents
wired over the message bus exactly as in paper trading, replaying real opening
prints and daily bars through a single simulated broker account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import AccountType, ActorId, Money, OmsType, TraderId

from lab import costs
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
from lab.agents.cross_sectional import CrossSectionalAgent, CrossSectionalAgentConfig
from lab.config import LabConfig, RiskSettings
from lab.metrics import summarize
from lab.signals.base import Signal
from lab.universe import NY, SIM, USD, equity, instrument_id, to_market_events


@dataclass
class Sleeve:
    signal: Signal
    ticker: str
    weight: float  # fraction of portfolio equity

    @property
    def id(self) -> str:
        return f"{self.signal.name}:{self.ticker}"


@dataclass
class RunResult:
    label: str
    equity: pd.Series
    metrics: dict
    fills: list
    risk_events: list
    gross: pd.Series
    orders_sent: int
    rejections: int
    signal_errors: int
    ledger_vs_account: float  # lab cash ledger minus simulator account cash (should be ~0)
    min_cash: float
    signals: list = field(default_factory=list)


def _ns(d: date) -> int:
    return int(pd.Timestamp(d).tz_localize(NY).value)


def _ns_close(d: date) -> int:
    return int((pd.Timestamp(d) + pd.Timedelta(hours=16)).tz_localize(NY).value)


def active_windows(frames: dict[str, pd.DataFrame], membership: dict | None) -> dict[str, list | None]:
    """Use ONLY independently supplied membership dates, never future price-file ends.

    An incomplete ticker series is not evidence of a known-in-advance delisting.
    If a stock stops trading before the common research endpoint, require a
    documented membership exit *and* later tradable bars to settle the exit.
    Otherwise the backtest is unpriceable and must be rejected.
    """
    ref_end = max(df.index[-1] for df in frames.values())
    out: dict[str, list | None] = {}
    for t, df in frames.items():
        ends_early = df.index[-1] < ref_end
        if ends_early:
            if len(df) < 3:
                raise ValueError(f"{t}: truncated history has no reliable final exit window")
            safe_last_exit = df.index[-3].date()
            spells = membership.get(t, []) if membership is not None else []
            if not spells or any(end is None or end > safe_last_exit for _, end in spells):
                raise ValueError(
                    f"{t}: price history ends on {df.index[-1].date()} before {ref_end.date()}; "
                    "cannot infer a prescient liquidation from the file endpoint. "
                    f"Supply independently verified point-in-time membership exits no later than {safe_last_exit}, "
                    "or exclude this dataset from validation."
                )
        if membership is None:
            out[t] = None
            continue
        windows = []
        for a, b in membership.get(t, []):
            windows.append((_ns(a) if a else 0, _ns_close(b) if b else None))
        out[t] = windows
    return out


def equal_weight(signal_factory, tickers: list[str]) -> list[Sleeve]:
    w = 1.0 / len(tickers)
    return [Sleeve(signal_factory(), t, w) for t in tickers]


def run(
    cfg: LabConfig,
    sleeves: list[Sleeve],
    data: dict[str, pd.DataFrame],
    start: date,
    end: date | None,
    *,
    label: str = "",
    risk: RiskSettings | None = None,
    capital: float | None = None,
    keep_signals: bool = False,
    warmup_start: date | None = None,
    execution_overrides: dict | None = None,
    membership: dict | None = None,
    normalize_active: bool = False,
    cross_sectional: list | None = None,
    target_vol: float = 0.0,
) -> RunResult:
    """sleeves: per-stock signals. cross_sectional: [(CrossSectionalSignal, weight, tickers)]."""
    cross_sectional = cross_sectional or []
    if not sleeves and not cross_sectional:
        raise ValueError("no sleeves")
    risk = risk or cfg.risk
    capital = capital or cfg.capital
    warm = pd.Timestamp(warmup_start or cfg.warmup_start)
    last = pd.Timestamp(end) if end else None
    tickers = sorted({s.ticker for s in sleeves} | {t for _, _, ts in cross_sectional for t in ts})

    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("LAB-001"), logging=LoggerConfig(stdout_level=LogLevel.ERROR))
    )
    try:
        engine.add_venue(
            SIM,
            OmsType.NETTING,
            AccountType.CASH,
            [Money(capital, USD)],
            base_currency=USD,
            fee_model=costs.fee_model(cfg.costs),
            fill_model=costs.fill_model(cfg.costs),
            latency_model=costs.latency_model(),
            allow_cash_borrowing=True,  # overnight gaps can briefly overdraw, as in a margin account
        )
        iids = {}
        frames = {}
        for t in tickers:
            if t not in data:
                raise ValueError(f"no data for {t}")
            df = data[t]
            df = df[(df.index >= warm) & ((df.index <= last) if last is not None else True)]
            if df.empty:
                raise ValueError(f"{t}: no data between {warm.date()} and {end}")
            iid = instrument_id(t, "SIM")
            iids[t] = iid
            frames[t] = df
            engine.add_instrument(equity(iid))
            engine.add_data(to_market_events(df, iid, cfg.costs.open_print_fraction))

        start_ns = _ns(start)
        windows = active_windows(frames, membership)
        signal_agents = []
        for i, s in enumerate(sleeves):
            a = SignalAgent(
                SignalAgentConfig(
                    actor_id=ActorId(f"Signal-{i:03d}-{s.ticker}"),
                    signal=s.signal,
                    sleeve=s.id,
                    ticker=s.ticker,
                    instrument_id=iids[s.ticker],
                    trade_start_ns=start_ns,
                    active_windows=windows[s.ticker],
                )
            )
            signal_agents.append(a)
            engine.add_actor(a)
        alloc_sleeves = {s.id: (s.ticker, s.weight) for s in sleeves}
        xs_agents = []
        for j, (sig, weight, xs_tickers) in enumerate(cross_sectional):
            a = CrossSectionalAgent(
                CrossSectionalAgentConfig(
                    actor_id=ActorId(f"CrossSectional-{j:02d}-{sig.name}"),
                    signal=sig,
                    instruments={t: iids[t] for t in xs_tickers},
                    trade_start_ns=start_ns,
                    active_windows={t: windows[t] for t in xs_tickers},
                )
            )
            xs_agents.append(a)
            engine.add_actor(a)
            for t in xs_tickers:
                alloc_sleeves[f"{sig.name}:{t}"] = (t, weight / len(xs_tickers))
        engine.add_actor(
            AllocatorAgent(
                AllocatorAgentConfig(
                    actor_id=ActorId("Allocator"),
                    sleeves=alloc_sleeves,
                    normalize_active=normalize_active,
                    absolute_groups={sig.name for sig, _, _ in cross_sectional},
                    target_vol=target_vol,
                    vol_window=cfg.portfolio.vol_window,
                )
            )
        )
        risk_agent = RiskAgent(
            RiskAgentConfig(
                actor_id=ActorId("Risk"),
                max_weight=risk.max_weight,
                max_gross=risk.max_gross,
                max_drawdown=risk.max_drawdown,
            )
        )
        engine.add_actor(risk_agent)
        recorder = RecorderAgent(RecorderAgentConfig(actor_id=ActorId("Recorder"), keep_signals=keep_signals))
        engine.add_actor(recorder)
        execution = ExecutionAgent(
            ExecutionAgentConfig(
                order_id_tag="EXEC",
                instruments=iids,
                venue=SIM,
                min_trade_value=cfg.costs.min_trade_value,
                rebalance_band=cfg.costs.rebalance_band,
                cash_buffer=cfg.costs.cash_buffer,
                capital=capital,
                **(execution_overrides or {}),
            )
        )
        engine.add_strategy(execution)

        engine.run()

        snaps = recorder.equity
        idx = pd.DatetimeIndex(
            [pd.Timestamp(s.ts, tz="UTC").tz_convert(NY).normalize().tz_localize(None) for s in snaps]
        )
        eq = pd.Series([s.equity for s in snaps], index=idx, dtype=float)
        gross = pd.Series([s.gross for s in snaps], index=idx, dtype=float)
        eq = eq[~eq.index.duplicated(keep="last")]
        gross = gross[~gross.index.duplicated(keep="last")]
        if len(eq):
            # Prepend the starting capital on the day before the first decision.
            eq = pd.concat([pd.Series([capital], index=[eq.index[0] - pd.Timedelta(days=1)]), eq])

        result = RunResult(
            label=label
            or ", ".join(sorted({s.signal.label() for s in sleeves} | {x[0].label() for x in cross_sectional})),
            equity=eq,
            metrics=summarize(eq, fills=recorder.fills, gross=gross),
            fills=recorder.fills,
            risk_events=recorder.risk,
            gross=gross,
            orders_sent=execution.orders_sent,
            rejections=execution.rejections,
            signal_errors=sum(a.errors for a in signal_agents) + sum(a.errors for a in xs_agents),
            ledger_vs_account=execution.cash - execution.account_cash(),
            min_cash=execution.min_cash,
            signals=recorder.signals,
        )
    finally:
        engine.dispose()
    return result
