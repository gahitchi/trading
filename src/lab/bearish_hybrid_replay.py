"""Offline, in-memory candle replay for the bearish hybrid candidate.

A conservative OHLC proxy, NOT order-book execution, partial fills, true
funding settlement, market impact, an authenticated feed or portfolio P&L.
The caller must pre-register trials and authenticate historical provenance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import isfinite

from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_replay import ReplayAssumptions, ShortTrade, _fill
from lab.bearish_hybrid import (
    MINUTE_NS,
    Bar,
    BearishHybrid,
    Costs,
    FundingEvent,
    MarketEligibility,
    OIObservation,
    Risk,
    Settings,
    VirtualAccount,
    paper_size,
)

DAY_NS = 1_440 * MINUTE_NS


@dataclass(frozen=True)
class HybridObservation:
    """One closed minute and caller-attested public-as-of inputs.

    `source_complete` is a caller assertion, not proof of archive integrity.
    An invalid minute during virtual inventory is an unresolved exposure.
    """

    bar: Bar
    market: MarketEligibility | None
    oi: OIObservation | None
    funding: tuple[FundingEvent, ...]
    funding_verified: bool
    source_complete: bool


@dataclass(frozen=True)
class HybridReplay:
    """Reference OHLC outcomes only; not equity, realized cash or P&L."""

    trades: tuple[ShortTrade, ...]
    virtual_notionals: tuple[float, ...]
    funded_signal_times_ns: tuple[int, ...]
    unfunded_signal_times_ns: tuple[int, ...]
    skipped_before_entry: int
    unresolved_after_entry: int
    halted: bool
    assumptions: ReplayAssumptions


@dataclass(frozen=True)
class _Pending:
    signal_ns: int
    entry_i: int
    stop: float
    target: float
    proposed_notional: float


@dataclass(frozen=True)
class _Active:
    signal_ns: int
    entry_i: int
    entry_price: float
    stop: float
    target: float
    virtual_notional: float


def replay_hybrid_observations(
    observations: Sequence[HybridObservation],
    *,
    boundary_ns: int,
    costs: Costs,
    account: VirtualAccount,
    settings: Settings | None = None,
    risk: Risk | None = None,
    assumptions: ReplayAssumptions | None = None,
) -> HybridReplay:
    """Scan only bars strictly before protected midnight, without I/O.

    After one FULL completed bar of delay, the next opening price is the
    earliest modeled entry. Both stop/target touched means stop first.
    Cost accounting uses the project's existing bearish replay kernel.

    `account` is a frozen research snapshot, not updated with trade returns;
    virtual notionals are hypothetical bounds, not a compounded ledger.
    Funding is an adverse-cost reserve, not an observed cash settlement.
    """
    if type(boundary_ns) is not int or boundary_ns <= 0 or boundary_ns % DAY_NS:
        raise ValueError("boundary must be a fixed positive UTC midnight in nanoseconds")
    account.check()
    s = settings or Settings()
    r = risk or Risk()
    a = assumptions or ReplayAssumptions()
    if a.entry_delay_full_minutes < 1:
        raise ValueError("hybrid replay requires at least one fully closed minute before entry")
    total_cost = costs.round_trip_bps
    historical_costs = RoundTripCosts(
        costs.fee_per_side_bps, costs.slippage_per_side_bps, costs.full_spread_bps, costs.adverse_funding_bps
    )
    if not isfinite(total_cost) or total_cost >= 10_000:
        raise ValueError("invalid replay costs")
    previous_ns = -1
    for obs in observations:
        if not isinstance(obs, HybridObservation) or not isinstance(obs.bar, Bar):
            raise ValueError("invalid historical observation")
        if type(obs.funding_verified) is not bool or type(obs.source_complete) is not bool:
            raise ValueError("source quality flags must be explicit booleans")
        ns = obs.bar.end_ns
        if type(ns) is not int or ns <= previous_ns or ns <= 0 or ns % MINUTE_NS or ns >= boundary_ns:
            raise ValueError("out-of-order, unaligned or protected holdout observation")
        previous_ns = ns

    engine = BearishHybrid(s, r)
    trades: list[ShortTrade] = []
    notionals: list[float] = []
    funded: list[int] = []
    unfunded: list[int] = []
    skipped = unresolved = 0
    pending: _Pending | None = None
    active: _Active | None = None
    previous_ns = -1
    halted = False

    for i, obs in enumerate(observations):
        bar = obs.bar
        gap = previous_ns != -1 and bar.end_ns - previous_ns != MINUTE_NS
        previous_ns = bar.end_ns
        if gap:
            if active is not None:
                unresolved += 1
                halted = True
                break
            if pending is not None:
                skipped += 1
                pending = None
            engine.reset()

        try:
            bar.check()
            valid = obs.source_complete
        except (ValueError, TypeError):
            valid = False
        if not valid:
            if active is not None or (pending is not None and i >= pending.entry_i):
                # Opening orders could have filled and cannot be safely reconstructed.
                unresolved += 1
                halted = True
                break
            if pending is not None:
                skipped += 1
                pending = None
            engine.reset()
            continue

        if pending is not None and i == pending.entry_i:
            opening = bar.open
            stop_bps = (pending.stop / opening - 1) * 10_000
            reward_bps = (opening - pending.target) / opening * 10_000
            if (
                stop_bps <= 0
                or stop_bps > s.max_stop_bps
                or reward_bps < s.target_cost_multiple * total_cost
                or pending.target >= opening
            ):
                skipped += 1
            else:
                size = min(pending.proposed_notional, paper_size(account, r, stop_bps, total_cost))
                if size <= 0:
                    skipped += 1
                else:
                    active = _Active(pending.signal_ns, i, opening, pending.stop, pending.target, size)
            pending = None

        if active is not None:
            reason = None
            if bar.high >= active.stop:
                reason = "stop"
                exit_price = max(bar.open, active.stop)
            elif bar.low <= active.target:
                reason = "target"
                exit_price = active.target
            elif i - active.entry_i + 1 >= a.max_hold_minutes:
                reason = "time"
                exit_price = bar.close
            if reason is not None:
                result = _fill(active.entry_price, exit_price, historical_costs)
                trades.append(
                    ShortTrade(
                        signal_end_ns=active.signal_ns,
                        entry_minute_start_ns=observations[active.entry_i].bar.end_ns - MINUTE_NS,
                        exit_bar_end_ns=bar.end_ns,
                        setup="impulse_pause_breakdown",
                        exit_reason=reason,
                        entry_reference=active.entry_price,
                        exit_reference=exit_price,
                        stop_reference=active.stop,
                        target_reference=active.target,
                        **result,
                    )
                )
                notionals.append(active.virtual_notional)
                active = None

        # The decision sees the entire CURRENT closed bar only now. The
        # entry price of a future bar is never passed into this decision.
        if active is not None or pending is not None:
            view = replace(account, open_positions=max(1, account.open_positions))
        else:
            view = account
        decision = engine.on_bar(
            bar,
            oi=obs.oi,
            market=obs.market,
            funding=obs.funding,
            funding_verified=obs.funding_verified,
            costs=costs,
            account=view,
        )
        if decision.action == "UNFUNDED_SIGNAL":
            unfunded.append(decision.at_ns)
        elif decision.action == "RESEARCH_SHORT":
            funded.append(decision.at_ns)
            if pending is not None or active is not None:
                # Defensive: no overlapping virtual exposure, ever.
                continue
            assert decision.stop_price is not None and decision.target_price is not None
            pending = _Pending(
                decision.at_ns,
                i + a.entry_delay_full_minutes + 1,
                decision.stop_price,
                decision.target_price,
                decision.virtual_notional,
            )

    if not halted:
        if active is not None:
            # An unclosed virtual position is NOT a completed winning trade.
            unresolved += 1
            halted = True
        if pending is not None:
            skipped += 1
    return HybridReplay(tuple(trades), tuple(notionals), tuple(funded), tuple(unfunded), skipped, unresolved, halted, a)
