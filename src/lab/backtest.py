"""
Run the full agent system over historical data in NautilusTrader's backtest engine.

One run = one portfolio: a set of sleeves (signal x ticker x weight), all agents
wired over the message bus exactly as in paper trading, replaying real opening
prints and bars through a single simulated broker account.

Speeds (lab.intraday): daily bars by default; at a quick speed ("1h", "15m", "5m") every bar is
decided on as soon as it reaches the agents (after the market-data delay) and filled at the next
opening print, with optional flat-at-close. Equity metrics are computed on session closes in both
cases, so daily and intraday results are comparable; the intraday drawdown is reported as well.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

import pandas as pd
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import AccountType, ActorId, Money, OmsType, TraderId

from lab import costs, intraday
from lab import data as lab_data
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
from lab.agents.schedule import Session
from lab.borrow import BorrowBook, BorrowUnavailable
from lab.config import LabConfig, RiskSettings
from lab.metrics import max_drawdown, summarize
from lab.signals.base import Signal
from lab.universe import (
    NY,
    SIM,
    USD,
    equity,
    instrument_id,
    sim_precision,
    to_intraday_events,
    to_market_events,
)


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
    financing: float = 0.0  # borrow fees + debit interest accrued (ASSUMED rates, lab.toml [margin])
    borrow_fees: float = 0.0
    margin_account: bool = False
    borrow_model: str = "assumed"
    borrow_fingerprint: str | None = None
    borrow_evidence: list = field(default_factory=list)
    speed: str = "daily"
    sessions: int = 0  # sessions traded (intraday history is short: say how much a result rests on)


def _ns(d: date) -> int:
    return int(pd.Timestamp(d).tz_localize(NY).value)


def _ns_close(d: date) -> int:
    return int((pd.Timestamp(d) + pd.Timedelta(hours=16)).tz_localize(NY).value)


def active_windows(frames: dict[str, pd.DataFrame], membership: dict | None) -> dict[str, list | None]:
    """Use declared membership only. Missing terminal data is not delisting evidence."""
    from lab.data import DataError

    ref_end = max(df.index[-1] for df in frames.values())
    out = {}
    for t, df in frames.items():
        spells = membership.get(t, []) if membership is not None else [(None, None)]
        ends_early = df.index[-1] < ref_end - pd.Timedelta(days=5)
        # A known membership exit is executable only when two subsequent real
        # bars remain (decision, then execution). Never invent an exit from EOF.
        exit_limit = df.index[-3].date() if len(df) >= 3 else date.min
        if ends_early and any(b is None or b > exit_limit for _, b in spells):
            raise DataError(
                f"{t}: terminal/delisting economics are not qualified; require a declared membership exit and subsequent real prices"
            )
        out[t] = None if membership is None else [(_ns(a) if a else 0, _ns_close(b) if b else None) for a, b in spells]
    return out


def settle_delistings(
    frames: dict[str, pd.DataFrame], membership: dict | None, delisted: dict | None
) -> tuple[dict[str, pd.DataFrame], dict | None, dict]:
    """Guard the boundary between exchange trading and corporate-action cash settlement.

    A recovery value is not an executable quote. In particular, there is no
    permissible post-delisting volume, ordinary market order, minimum positive
    recovery price, or synthetic closeout fill. An explicit native Nautilus
    position/account plus independent-ledger settlement integration is required
    before delisted names can participate in a qualified backtest.

    This fail-closed guard intentionally prevents misleading research results;
    callers must not backdate membership exits to circumvent it.
    """
    from lab.data import DataError

    hits = {t: d for t, d in (delisted or {}).items() if t in frames}
    if not hits:
        return frames, membership, {}
    for ticker, record in hits.items():
        last_bar = frames[ticker].index[-1]
        delist_date = pd.Timestamp(record["delist_date"])
        if delist_date > last_bar:
            continue  # Future event: never invent a trade or change the feed.
        if delist_date != last_bar:
            raise DataError(
                f"{ticker}: prices continue past its delisting date {delist_date.date()}; "
                "fix the data or the corporate-action record"
            )
        raise DataError(
            f"{ticker}: delisting settlement requires a native-account and ledger "
            "corporate-action event; post-delisting market fills are forbidden. "
            "The supplied recovery price is not executable liquidity."
        )
    return frames, membership, {}

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
    speed: str = "daily",
    bank=None,
) -> RunResult:
    """sleeves: per-stock signals. cross_sectional: [(CrossSectionalSignal, weight, tickers)].
    speed: "daily" (data: daily frames) or a quick speed (data: lab.intraday frames of that bar size)."""
    from lab.config import validate_effective

    validate_effective(cfg)
    cross_sectional = cross_sectional or []
    if not sleeves and not cross_sectional:
        raise ValueError("no sleeves")
    ids = [s.id for s in sleeves] + [f"{sig.name}:{t}" for sig, _, tickers in cross_sectional for t in tickers]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sleeve identity: use each signal name once per ticker")
    risk = risk or cfg.risk
    capital = cfg.capital if capital is None else capital
    validate_effective(
        replace(cfg, risk=risk, capital=capital, portfolio=replace(cfg.portfolio, target_vol=target_vol))
    )
    sp = intraday.speed(speed)
    quick = not sp.daily
    if bank is not None and quick:
        raise ValueError("strategy bank supports daily policy evaluation only")
    if quick:
        try:
            intraday.check_speed_allowed(speed, cfg.risk.profile)
        except ValueError as e:
            raise ValueError(f"{e} (Gabriel's rule: quick trades are for the dynamic and aggressive profiles)") from e
    daily_only = [x.signal.name for x in sleeves if getattr(x.signal, "intraday", False)] if not quick else []
    if daily_only:
        raise ValueError(f"{', '.join(sorted(set(daily_only)))}: intraday technique(s); they need a quick speed")
    # Delayed data (minutes) rounded up to whole bars: a decision is made when the bar is known, never sooner.
    delay_bars = math.ceil(cfg.trading.data_delay_min / sp.minutes) if quick else 0
    if quick:
        for sleeve in sleeves:
            intraday.check_technique(
                sleeve.signal.name, sleeve.signal.params, sp.minutes, delay_bars * sp.minutes, cfg.trading.flat_at_close
            )
    bars_per_year = 252 * sp.bars_per_session
    p = cfg.portfolio
    # Shorts and leverage need a margin account (a cash account refuses short sales, as IBKR's would);
    # a long-only, unlevered run keeps the cash account it always had.
    margin_book = risk.max_short_gross > 0 or risk.max_gross > 1 or p.leverage > 1 or p.max_leverage > 1
    warm = pd.Timestamp(warmup_start or cfg.warmup_start)
    last = pd.Timestamp(end) + pd.Timedelta(days=1) if end else None  # exclusive: all of the end date
    tickers = sorted({s.ticker for s in sleeves} | {t for _, _, ts in cross_sectional for t in ts})
    borrow_book = None
    if cfg.borrow.history is not None:
        try:
            borrow_book = BorrowBook.from_csv(cfg.borrow.history, max_age_days=cfg.borrow.max_age_days)
        except (OSError, ValueError) as exc:
            raise BorrowUnavailable(f"cannot load borrow history {cfg.borrow.history}: {exc}") from exc
    if "borrow_book" in (execution_overrides or {}):
        raise ValueError("configure borrow history through cfg.borrow, not execution_overrides")

    data, membership, settlements = settle_delistings(
        {t: data[t] for t in tickers if t in data}, membership, lab_data.delistings(cfg)
    )
    manifest = lab_data.manifest(cfg)
    splits = {
        t: [(_ns(date.fromisoformat(d)), float(r)) for d, r in manifest.get(t, {}).get("splits", [])] for t in tickers
    }
    fees = costs.fee_model(
        cfg.costs, {t: costs.MarketProfile.from_bars(df, cfg.costs) for t, df in data.items()}, splits
    )
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("LAB-001"), logging=LoggerConfig(stdout_level=LogLevel.ERROR))
    )
    try:
        venue_kw = (
            {"default_leverage": Decimal(1) / Decimal(str(min(cfg.margin.initial_long, cfg.margin.initial_short)))}
            if margin_book
            else {"allow_cash_borrowing": True}  # overnight gaps can briefly overdraw, as in a margin account
        )
        if quick:
            # Fills only at opening prints: a bar reaching the agents late (delayed data) must never be a price
            # an order fills at.
            venue_kw["bar_execution"] = False
        engine.add_venue(
            SIM,
            OmsType.NETTING,
            AccountType.MARGIN if margin_book else AccountType.CASH,
            [Money(capital, USD)],
            base_currency=USD,
            fee_model=fees,
            fill_model=costs.fill_model(cfg.costs),
            latency_model=costs.latency_model(),
            **venue_kw,
        )
        iids = {}
        frames = {}
        for t in tickers:
            if t not in data:
                raise ValueError(f"no data for {t}")
            df = data[t]
            df = df[(df.index >= warm) & ((df.index < last) if last is not None else True)]
            if df.empty:
                raise ValueError(f"{t}: no data between {warm.date()} and {end}")
            iid = instrument_id(t, "SIM")
            iids[t] = iid
            frames[t] = df
            engine.add_instrument(equity(iid, sim_precision(df)))
            if quick:
                engine.add_data(
                    to_intraday_events(df, iid, cfg.costs.open_print_fraction, sp.minutes, sp.bar_spec(), delay_bars)
                )
            else:
                engine.add_data(to_market_events(df, iid, cfg.costs.open_print_fraction))

        start_ns = _ns(start)
        windows = active_windows(frames, membership)
        session = Session(sp.minutes, delay_min=delay_bars * sp.minutes, fetch_lag_s=0, decide_s=1) if quick else None
        speed_kw = (
            {
                "bar_spec": sp.bar_spec(),
                "session": session,
                # The latest bar must be this interval's: no older than the delay plus one bar, plus slack.
                "max_bar_age_days": session.max_bar_age().total_seconds() / 86_400,
            }
            if quick
            else {}
        )
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
                    **speed_kw,
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
                    **speed_kw,
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
                    # Intraday: at least five sessions of bars, so one quiet morning cannot lever the book up.
                    vol_window=max(
                        cfg.portfolio.vol_window, bank.window if bank else 0, 5 * sp.bars_per_session if quick else 0
                    ),
                    bank=bank,
                    max_leverage=p.max_leverage,
                    leverage=p.leverage,
                    periods_per_year=bars_per_year,
                    weighting=cfg.portfolio.weighting,
                    session_bars=sp.bars_per_session if quick else 1,
                    flat_at_close=quick and cfg.trading.flat_at_close,
                )
            )
        )
        risk_agent = RiskAgent(
            RiskAgentConfig(
                actor_id=ActorId("Risk"),
                max_weight=risk.max_weight,
                max_gross=risk.max_gross,
                max_drawdown=risk.max_drawdown,
                max_short_gross=risk.max_short_gross,
                min_net=risk.min_net,
                max_net=risk.max_net,
                margin=cfg.margin,
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
                margin=cfg.margin,
                borrow_book=borrow_book,
                session=session,
                flat_at_close=quick and cfg.trading.flat_at_close,
                max_gross=risk.max_gross,
                risk_limits=risk,
                reservation_costs=cfg.costs,
                reservation_gap=max(cfg.paper.reservation_gap, cfg.trading.limit_collar),
                entry_slippage=cfg.trading.limit_collar,
                limit_collar=cfg.trading.limit_collar,
                max_participation=cfg.costs.max_participation,
                participation_window=cfg.costs.cost_window,
                **(execution_overrides or {}),
            )
        )
        engine.add_strategy(execution)

        engine.run()
        if execution.borrow_error:
            raise BorrowUnavailable(f"backtest invalid: {execution.borrow_error}")

        # The engine stops at its last market event; a timer one second later
        # never fires. Still value the final fills at the final completed marks.
        if execution.price_ts:
            unsafe = execution._unsafe_held_marks()
            if unsafe:
                raise ValueError(f"cannot value final held positions: {unsafe}")
            recorder.equity.append(execution.snapshot())
        snaps = recorder.equity
        idx = pd.DatetimeIndex(
            [pd.Timestamp(s.ts, tz="UTC").tz_convert(NY).normalize().tz_localize(None) for s in snaps]
        )
        eq = pd.Series([s.equity for s in snaps], index=idx, dtype=float)
        every = pd.Series([s.equity for s in snaps], dtype=float)  # every decision, intraday included
        gross = pd.Series([s.gross for s in snaps], index=idx, dtype=float)
        eq = eq[~eq.index.duplicated(keep="last")]
        gross = gross[~gross.index.duplicated(keep="last")]
        if len(eq):
            # Prepend the starting capital on the day before the first decision.
            eq = pd.concat([pd.Series([capital], index=[eq.index[0] - pd.Timedelta(days=1)]), eq])

        metrics = summarize(eq, fills=recorder.fills, gross=gross, risk_free=lab_data.risk_free(cfg))
        if quick and len(every) > 2:
            metrics["intraday_max_drawdown"] = max_drawdown(pd.concat([pd.Series([capital]), every]))[0]
        # The fills' fees include the modelled spread and impact; report each part (F-R06).
        metrics["commissions"] = round(fees.totals["commission"], 2)
        metrics["spread_costs"] = round(fees.totals["spread"], 2)
        metrics["impact_costs"] = round(fees.totals["impact"], 2)
        if settlements:
            metrics["delistings_settled"] = settlements
            # Compatibility bars close Nautilus positions via simulated fills,
            # but their supplied market volume is NOT historical exchange depth.
            # Useful for diagnostic research, never qualified executable evidence.
            metrics["execution_qualified"] = False
            metrics["delisting_execution_model"] = "synthetic_settlement_bars_not_executable"
        # Realized hard-limit breaches at decision marks, reported apart from the limits themselves (G013-F2).
        kinds = [e.kind for e in recorder.risk]
        metrics["cap_breach_decisions"] = kinds.count("cap_breach")
        metrics["cap_breach_unresolved"] = kinds.count("cap_breach_unresolved")
        result = RunResult(
            label=label
            or ", ".join(sorted({s.signal.label() for s in sleeves} | {x[0].label() for x in cross_sectional})),
            equity=eq,
            metrics=metrics,
            fills=recorder.fills,
            risk_events=recorder.risk,
            gross=gross,
            orders_sent=execution.orders_sent,
            rejections=execution.rejections,
            signal_errors=sum(a.errors for a in signal_agents) + sum(a.errors for a in xs_agents),
            ledger_vs_account=execution.cash - execution.account_cash(),
            min_cash=execution.min_cash,
            signals=recorder.signals,
            financing=execution.financing,
            borrow_fees=execution.borrow_fees,
            margin_account=margin_book,
            borrow_model="historical_close_book" if borrow_book is not None else "assumed",
            borrow_fingerprint=borrow_book.fingerprint() if borrow_book is not None else None,
            borrow_evidence=execution.borrow_ledger.evidence if execution.borrow_ledger is not None else [],
            speed=sp.name,
            sessions=len(eq) - 1 if len(eq) else 0,
        )
    finally:
        engine.dispose()
        for signal in [s.signal for s in sleeves] + [s for s, _, _ in cross_sectional]:
            if hasattr(signal, "close"):
                signal.close()
    return result
