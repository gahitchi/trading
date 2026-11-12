"""Causal, simulated-only short replay with realized-profit allocation.

Never sends orders, models market depth, authenticates market sources, or
claims real cash. All trade sizing happens using *earlier realized* virtual
cash, not later price moves. Unrealized equity is used for daily risk/marks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose, isfinite

from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_replay import ReplayAssumptions, ShortTrade, _fill
from lab.bearish_equity import _closeout
from lab.bearish_hybrid import Bar, BearishHybrid, Costs, Risk, Settings, VirtualAccount, paper_size
from lab.bearish_hybrid_ledger import VirtualDay
from lab.bearish_hybrid_replay import DAY_NS, MINUTE_NS, HybridObservation


@dataclass(frozen=True)
class ProfitPolicy:
    """Fraction of positive realized virtual net P&L available for later sizing.

    Unreinvested positive P&L remains as **virtual protected reserve**;
    all negative net P&L is debited from the trading allocation. 0 means
    fixed initial risk capital with gains reserved; 1 reinvests all gains.
    """

    reinvest_positive_fraction: float

    def __post_init__(self) -> None:
        v = self.reinvest_positive_fraction
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or not 0 <= v <= 1:
            raise ValueError("reinvestment fraction must be a finite value between zero and one")


@dataclass(frozen=True)
class DynamicHybridReport:
    trades: tuple[ShortTrade, ...]
    virtual_notionals: tuple[float, ...]
    trading_equity_at_entries: tuple[float, ...]
    days: tuple[VirtualDay, ...]
    initial_virtual_equity: float
    ending_trading_cash: float
    reserved_virtual_profits: float
    ending_total_virtual_equity: float
    completed_trades: int
    skipped_entries: int
    candidate_signals: int
    max_daily_drawdown: float
    positive_days: int
    negative_days: int
    flat_days: int
    qualified: bool = False
    actual_notional: float = 0.0


@dataclass(frozen=True)
class _Pending:
    signal_ns: int
    entry_index: int
    stop: float
    target: float
    signal_notional: float


@dataclass(frozen=True)
class _Active:
    signal_ns: int
    entry_index: int
    entry_ref: float
    stop: float
    target: float
    notional: float
    entry_fill: float


def replay_hybrid_dynamic(
    observations: Sequence[HybridObservation],
    *,
    boundary_ns: int,
    costs: Costs,
    account: VirtualAccount,
    profit_policy: ProfitPolicy,
    risk: Risk | None = None,
    settings: Settings | None = None,
    assumptions: ReplayAssumptions | None = None,
) -> DynamicHybridReport:
    """Forward-only OHLC virtual cash and daily mark-to-market accounting.

    Require full UTC days, unbroken and explicit source-quality data, and a
    *separate funded virtual* account. Missing/ambiguous history rejects
    the whole claim; use zero-capital signal-only replay for an empty account.
    The USD-M funding debit is a conservative fixed reserve, NOT an actual
    exchange funding payment. No actual leverage/liquidation is modeled.
    """
    account.check()
    if (
        account.equity <= 0
        or not isclose(account.equity, account.day_start_equity, rel_tol=0, abs_tol=1e-9)
        or account.day_pnl != 0
        or account.current_gross_notional != 0
        or account.open_positions != 0
        or account.free_collateral > account.equity
    ):
        raise ValueError("dynamic replay requires initially flat, funded virtual cash and collateral")
    if not isinstance(profit_policy, ProfitPolicy):
        raise ValueError("explicit positive-profit allocation policy required")
    if type(boundary_ns) is not int or boundary_ns <= 0 or boundary_ns % DAY_NS:
        raise ValueError("protected boundary must be a positive UTC midnight")
    s = settings or Settings()
    r = risk or Risk()
    a = assumptions or ReplayAssumptions()
    if a.entry_delay_full_minutes < 1:
        raise ValueError("at least one complete intervening minute is required before entry")
    all_cost = costs.round_trip_bps
    if not 0 <= all_cost < 10_000:
        raise ValueError("invalid modeled costs")
    round_trip = RoundTripCosts(
        costs.fee_per_side_bps, costs.slippage_per_side_bps, costs.full_spread_bps, costs.adverse_funding_bps
    )
    if (
        not observations
        or not isinstance(observations[0], HybridObservation)
        or not isinstance(observations[-1], HybridObservation)
        or (observations[0].bar.end_ns - MINUTE_NS) % DAY_NS
        or observations[-1].bar.end_ns % DAY_NS
        or len(observations) % 1_440
    ):
        raise ValueError("complete UTC day windows (00:01 through next 00:00) required")

    previous_ns = -1
    for obs in observations:
        if (
            not isinstance(obs, HybridObservation)
            or not isinstance(obs.bar, Bar)
            or obs.source_complete is not True
            or type(obs.funding_verified) is not bool
        ):
            raise ValueError("invalid or untrusted full-day market observation")
        obs.bar.check()
        ns = obs.bar.end_ns
        if ns >= boundary_ns or (previous_ns >= 0 and ns != previous_ns + MINUTE_NS):
            raise ValueError("protected holdout or incomplete continuous minute prices")
        previous_ns = ns

    detector = BearishHybrid(s, r)
    capital = account.equity
    reserve = 0.0
    collateral_ratio = account.free_collateral / account.equity
    day_start_trading_mark = capital
    reserve_at_day_start = 0.0
    previous_daily_equity = capital
    peak = capital
    worst_daily_drawdown = 0.0
    pending: _Pending | None = None
    active: _Active | None = None
    trades: list[ShortTrade] = []
    notionals: list[float] = []
    entry_equities: list[float] = []
    days: list[VirtualDay] = []
    entries_today = exits_today = 0
    skipped = candidate_signals = 0
    fee_rate = costs.fee_per_side_bps / 10_000

    def marked_trading_cash(bar: Bar) -> float:
        if active is None:
            return capital
        qty = active.notional / active.entry_ref
        cover = _closeout(bar.close, round_trip)
        entry_fee = qty * active.entry_fill * fee_rate
        exit_fee = qty * cover * fee_rate
        return (
            capital
            + qty * (active.entry_fill - cover)
            - entry_fee
            - exit_fee
            - active.notional * costs.adverse_funding_bps / 10_000
        )

    for i, obs in enumerate(observations):
        bar = obs.bar
        if pending is not None and i == pending.entry_index:
            # Delayed opening reference is used exclusively at this time.
            stop_bps = (pending.stop / bar.open - 1) * 10_000
            reward_bps = (bar.open - pending.target) / bar.open * 10_000
            if (
                stop_bps <= 0
                or stop_bps > s.max_stop_bps
                or pending.target <= 0
                or reward_bps < s.target_cost_multiple * all_cost
            ):
                skipped += 1
            else:
                view = VirtualAccount(
                    equity=capital,
                    day_start_equity=day_start_trading_mark,
                    day_pnl=capital + reserve - reserve_at_day_start - day_start_trading_mark,
                    free_collateral=capital * collateral_ratio,
                    kill_mode=account.kill_mode,
                    risk_ready=account.risk_ready,
                )
                size = min(pending.signal_notional, paper_size(view, r, stop_bps, all_cost))
                if size <= 0:
                    skipped += 1
                else:
                    entry_fill = bar.open * (1 - costs.full_spread_bps / 20_000) * (
                        1 - costs.slippage_per_side_bps / 10_000
                    )
                    active = _Active(
                        pending.signal_ns, i, bar.open, pending.stop, pending.target, size, entry_fill
                    )
                    notionals.append(size)
                    entry_equities.append(capital)
                    entries_today += 1
            pending = None

        if active is not None:
            qty = active.notional / active.entry_ref
            worst_cover = _closeout(bar.high, round_trip)
            worst_exit_fee = qty * worst_cover * fee_rate
            worst_equity = (
                capital
                + qty * (active.entry_fill - worst_cover)
                - qty * active.entry_fill * fee_rate
                - worst_exit_fee
                - active.notional * costs.adverse_funding_bps / 10_000
            )
            if worst_equity <= 0:
                raise ValueError("unknown intrabar margin or virtual insolvency; no equity claim")
            reason = None
            if bar.high >= active.stop:
                reason = "stop"
                exit_ref = max(bar.open, active.stop)
            elif bar.low <= active.target:
                reason = "target"
                exit_ref = active.target
            elif i - active.entry_index + 1 >= a.max_hold_minutes:
                reason = "time"
                exit_ref = bar.close
            if reason:
                execution = _fill(active.entry_ref, exit_ref, round_trip)
                trade = ShortTrade(
                    signal_end_ns=active.signal_ns,
                    entry_minute_start_ns=observations[active.entry_index].bar.end_ns - MINUTE_NS,
                    exit_bar_end_ns=bar.end_ns,
                    setup="impulse_pause_breakdown",
                    exit_reason=reason,
                    entry_reference=active.entry_ref,
                    exit_reference=exit_ref,
                    entry_fill=execution["entry_fill"],
                    exit_fill=execution["exit_fill"],
                    stop_reference=active.stop,
                    target_reference=active.target,
                    gross_return=execution["gross_return"],
                    net_return=execution["net_return"],
                    fee_cost=execution["fee_cost"],
                    slippage_cost=execution["slippage_cost"],
                    spread_cost=execution["spread_cost"],
                    funding_reserve_cost=execution["funding_reserve_cost"],
                )
                pnl = active.notional * trade.net_return
                if pnl >= 0:
                    capital += pnl * profit_policy.reinvest_positive_fraction
                    reserve += pnl * (1 - profit_policy.reinvest_positive_fraction)
                else:
                    capital += pnl
                if capital <= 0 or not isfinite(capital + reserve):
                    raise ValueError("virtual capital exhausted; no liquidation available")
                trades.append(trade)
                exits_today += 1
                active = None

        # Midnight marks and risk-day reset occur after the completed candle
        # but BEFORE decisions at midnight that belong to the next UTC day.
        if bar.end_ns % DAY_NS == 0:
            trading_mark = marked_trading_cash(bar)
            total_mark = trading_mark + reserve
            if trading_mark <= 0 or not isfinite(total_mark):
                raise ValueError("unqualified mark-to-market virtual insolvency")
            change = total_mark - previous_daily_equity
            label = bar.end_ns - MINUTE_NS
            day = datetime.fromtimestamp(label / 1_000_000_000, tz=UTC).date().isoformat()
            days.append(
                VirtualDay(
                    day=day,
                    start_equity=previous_daily_equity,
                    end_equity=total_mark,
                    net_pnl=change,
                    daily_return=change / previous_daily_equity,
                    realized_cash=capital + reserve,
                    unrealized_after_closeout=total_mark - capital - reserve,
                    entries=entries_today,
                    exits=exits_today,
                    open_position=active is not None,
                )
            )
            previous_daily_equity = total_mark
            peak = max(peak, total_mark)
            worst_daily_drawdown = max(worst_daily_drawdown, (peak - total_mark) / peak)
            day_start_trading_mark = trading_mark
            reserve_at_day_start = reserve
            entries_today = exits_today = 0

        trading_mark = marked_trading_cash(bar)
        pnl_since_midnight = trading_mark + reserve - reserve_at_day_start - day_start_trading_mark
        free = max(0.0, capital * collateral_ratio - (active.notional * r.margin_fraction if active else 0))
        view = VirtualAccount(
            equity=capital,
            day_start_equity=day_start_trading_mark,
            day_pnl=pnl_since_midnight,
            current_gross_notional=active.notional if active else (pending.signal_notional if pending else 0),
            free_collateral=free,
            open_positions=1 if active is not None or pending is not None else 0,
            kill_mode=account.kill_mode,
            risk_ready=account.risk_ready,
        )
        decision = detector.on_bar(
            bar,
            oi=obs.oi,
            market=obs.market,
            funding=obs.funding,
            funding_verified=obs.funding_verified,
            costs=costs,
            account=view,
        )
        if decision.action == "RESEARCH_SHORT":
            candidate_signals += 1
            if pending is not None or active is not None:
                raise ValueError("overlapping virtual position intent")
            assert decision.stop_price is not None and decision.target_price is not None
            pending = _Pending(
                signal_ns=bar.end_ns,
                entry_index=i + a.entry_delay_full_minutes + 1,
                stop=decision.stop_price,
                target=decision.target_price,
                signal_notional=decision.virtual_notional,
            )

    if active is not None:
        raise ValueError("unresolved terminal short inventory; no completed daily equity claim")
    if pending is not None:
        skipped += 1
    if len(notionals) != len(trades):
        raise ValueError("unresolved entry/outcome pairing")
    if not isclose(days[-1].end_equity, capital + reserve, rel_tol=1e-10, abs_tol=1e-8):
        raise ValueError("terminal account cash does not reconcile to daily marks")
    return DynamicHybridReport(
        trades=tuple(trades),
        virtual_notionals=tuple(notionals),
        trading_equity_at_entries=tuple(entry_equities),
        days=tuple(days),
        initial_virtual_equity=account.equity,
        ending_trading_cash=capital,
        reserved_virtual_profits=reserve,
        ending_total_virtual_equity=capital + reserve,
        completed_trades=len(trades),
        skipped_entries=skipped,
        candidate_signals=candidate_signals,
        max_daily_drawdown=worst_daily_drawdown,
        positive_days=sum(d.net_pnl > 0 for d in days),
        negative_days=sum(d.net_pnl < 0 for d in days),
        flat_days=sum(d.net_pnl == 0 for d in days),
    )
