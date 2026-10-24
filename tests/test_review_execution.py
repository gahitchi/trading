"""Order-transition regressions from the follow-up audit, using the real rebalance method."""

from types import SimpleNamespace

import pytest

from lab.agents.execution import ExecutionAgent


def _agent(held, weights, max_gross=None, risk_limits=None):
    from lab.config import MarginSettings

    sent = []
    cfg = SimpleNamespace(
        max_gross=max_gross,
        risk_limits=risk_limits,
        margin=MarginSettings(),
        entry_slippage=0.01,
        live=True,
        session=None,
        flat_at_close=False,
        instruments={t: t for t in held},
        borrow_book=None,
        cash_buffer=0,
        min_trade_value=0,
        rebalance_band=0,
        max_order_value=100_000,
        max_orders_per_day=100,
        max_daily_turnover=10,
    )
    agent = SimpleNamespace(
        _reserve=lambda destinations, equity: ExecutionAgent._reserve(agent, destinations, equity),
        config=cfg,
        borrow_error=None,
        borrow_ledger=None,
        held=held,
        weights=weights,
        prices=dict.fromkeys(held, 100),
        price_ts=0,
        pending={},
        halted=False,
        reduce_only=False,
        cap_day=None,
        cap_orders=0,
        cap_opened=0,
        liquidation_requested=False,
        _consume_adopt=lambda: False,
        reconcile=lambda: [],
        _unsafe_held_marks=lambda: [],
        snapshot=lambda: SimpleNamespace(equity=1000),
        publish_message=lambda *_: None,
        _halt=lambda: None,
        _kill=lambda: None,
        _session_phase=lambda: None,
        _check_funding=lambda _: True,
        _risk_event=lambda *_: None,
        _save=lambda: None,
        _submit=lambda iid, ticker, diff, **kw: sent.append((ticker, diff, kw.get("adds", False))) or True,
        cache=SimpleNamespace(orders_open_count=lambda **_: 0),
    )
    return agent, sent


def test_rejected_or_unfilled_reduction_cannot_finance_another_entry():
    agent, sent = _agent({"A": 10, "B": 0}, {"A": 0, "B": 1})
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", -10, False)]  # no assumption that sending A's exit frees capital for B


def test_pending_order_elsewhere_blocks_additions():
    agent, sent = _agent({"A": 0, "B": 0}, {"A": 0, "B": 1})
    agent.pending = {"unknown": {"ticker": "A"}}
    ExecutionAgent.rebalance(agent)
    assert sent == []


def test_reversal_exits_first_without_opening_the_other_side():
    agent, sent = _agent({"A": -5}, {"A": 1})
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", 5, False)]


def test_cash_halt_and_missing_marks_cannot_block_requested_quantity_exit():
    agent, sent = _agent({"A": 5}, {"A": 1})
    agent._halt = lambda: {"kind": "cash_mismatch"}
    agent._kill = lambda: {"mode": "flatten"}
    agent._unsafe_held_marks = lambda: ["A"]
    agent._check_funding = lambda _: (_ for _ in ()).throw(AssertionError("exit must not require funding"))
    agent._liquidate = lambda: ExecutionAgent._liquidate(agent)
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", -5, False)]


def test_gross_headroom_does_not_bypass_settlement_of_reductions():
    agent, sent = _agent({"A": 3, "B": 0}, {"A": 0, "B": 0.3}, max_gross=1.0)
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", -3, False)]


def test_a_full_rotation_waits_for_the_exit_to_settle():
    agent, sent = _agent({"A": 8, "B": 0}, {"A": 0, "B": 1.0}, max_gross=1.0)
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", -8, False)]


def _limits(profile):
    from lab.config import RiskSettings

    return {  # the profiles' tables (lab.risk_profiles)
        "balanced": RiskSettings(0.25, 1.0, 0.18, max_short_gross=0.0, min_net=0.0, max_net=1.0),
        "aggressive": RiskSettings(0.45, 1.8, 0.30, max_short_gross=0.75, min_net=-0.75, max_net=1.6),
    }[profile]


def test_an_entry_that_fits_every_fill_subset_goes_out_beside_an_unfilled_reduction():
    """Claude on G009/G010-F8: with the limits known, lab.reservations bounds every subset of fills, so an
    entry that fits even if the reduction never fills need not wait for it."""
    agent, sent = _agent({"A": 1, "B": 0}, {"A": 0, "B": 0.2}, risk_limits=_limits("balanced"))
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", -1, False), ("B", 2, True)]  # 10% in A, 20% into B: at worst 30% gross


def test_the_reservation_check_still_refuses_what_a_failed_reduction_would_break():
    rotation, sent = _agent({"A": 2, "B": 0}, {"A": 0, "B": 0.25}, risk_limits=_limits("balanced"))
    ExecutionAgent.rebalance(rotation)
    assert sent == [("A", -2, False), ("B", 2, True)]  # worst 45% gross and 25% B: inside balanced
    short, sent = _agent({"A": -5, "B": 0}, {"A": 0, "B": -0.5}, risk_limits=_limits("aggressive"))
    ExecutionAgent.rebalance(short)
    assert sent == [("A", 5, False)]  # ChatGPT's counterexample: short gross 1.0 > 0.75 if A's cover fails


def test_live_financing_is_carried_by_the_estimate_unless_the_broker_cash_books_it():
    """Allocation mode: the broker charges the whole account and the lab's cash never sees it, so the estimate
    is the lab's financing. Dedicated mode books the actual charge into cash, so the estimate is not deducted."""
    from lab.agents.execution import ExecutionAgent as EA

    def equity(dedicated):
        agent = SimpleNamespace(
            config=SimpleNamespace(live=True, dedicated_account=dedicated),
            cash=10_000.0,
            financing=12.5,
            min_cash=float("inf"),
            price_ts=1,
            held={"A": -10},
            _accrue=lambda: None,
            _book=lambda: (0.0, 1_000.0),
        )
        return EA.snapshot(agent).equity

    assert equity(dedicated=False) == pytest.approx(10_000 - 1_000 - 12.5)
    assert equity(dedicated=True) == pytest.approx(10_000 - 1_000)


def test_daily_caps_are_reserved_before_order_submission():
    agent, _ = _agent({"A": 0}, {"A": 0.2}, risk_limits=_limits("balanced"))
    saved_before_send = []

    def crash_after_send(*args, **kwargs):
        saved_before_send.append((agent.cap_orders, agent.cap_opened))
        raise OSError("process interrupted immediately after transmission")

    agent._submit = crash_after_send
    with pytest.raises(OSError):
        ExecutionAgent.rebalance(agent)
    assert saved_before_send == [(1, 200)]
    assert agent.cap_orders == 1 and agent.cap_opened == 200


def test_local_submission_refusal_does_not_consume_daily_caps():
    agent, _ = _agent({"A": 0}, {"A": 0.2}, risk_limits=_limits("balanced"))
    agent._submit = lambda *args, **kwargs: False
    ExecutionAgent.rebalance(agent)
    assert agent.cap_orders == 0 and agent.cap_opened == 0


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_broker_positions_never_reconcile(quantity):
    a, _ = _agent({"A": 0}, {"A": 0})
    a._broker_position = lambda ticker: quantity
    assert ExecutionAgent.reconcile(a)


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1])
def test_invalid_cost_reservations_fail_closed(cost):
    from lab.config import MarginSettings
    from lab.reservations import check_openings

    assert check_openings({}, {"A": 1}, {"A": 100}, 1000, _limits("balanced"), MarginSettings(), cost)


def test_nan_position_cannot_bypass_reservation_limits():
    from lab.config import MarginSettings
    from lab.reservations import check_openings

    assert check_openings({"A": float("nan")}, {"A": 1}, {"A": 100}, 1000, _limits("balanced"), MarginSettings())


def test_cost_and_gap_reserves_resize_entry_before_submission(goog_lab):
    from dataclasses import replace

    from lab.reservations import stressed_openings

    limits = replace(goog_lab.risk, max_weight=1, max_gross=1, max_net=1)
    agent, sent = _agent({"A": 0}, {"A": 1}, risk_limits=limits)
    agent.config.reservation_costs = goog_lab.costs
    agent.config.reservation_gap = 0.03
    ExecutionAgent.rebalance(agent)
    assert sent and 0 < sent[0][1] < 10
    assert (
        stressed_openings(
            {"A": 0}, {"A": sent[0][1]}, {"A": 100}, 1000, limits, agent.config.margin, goog_lab.costs, 0.03
        )
        is None
    )
    assert stressed_openings({"A": 0}, {"A": 10}, {"A": 100}, 1000, limits, agent.config.margin, goog_lab.costs, 0.03)


def test_alert_outage_blocks_additions_but_permits_reductions():
    agent, sent = _agent({"A": 10, "B": 0}, {"A": 0, "B": 1}, risk_limits=_limits("balanced"))
    agent.config.notification_health = lambda: {"healthy": False}
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", -10, False)]
    assert "notification delivery" in agent.blocked_reason


def test_manual_targets_replace_signal_targets_without_bypassing_limits(tmp_path):
    from lab.authority import Authority

    authority = Authority(tmp_path, {"tickers": ["A", "B"]})
    authority.issue("manual", targets={"A": 0.1})
    agent, sent = _agent({"A": 0, "B": 0}, {"A": 0, "B": 0.2}, risk_limits=_limits("balanced"))
    agent.config.authority = authority
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", 1, True)]


def test_reservation_gap_changes_research_identity(goog_lab):
    from dataclasses import replace

    from lab.experiments import config_fingerprint
    from lab.research.approval import deployment_context

    changed = replace(goog_lab, paper=replace(goog_lab.paper, reservation_gap=0.1))
    assert config_fingerprint(changed) != config_fingerprint(goog_lab)
    assert deployment_context(changed.snapshot()) != deployment_context(goog_lab.snapshot())


def test_independent_adverse_marks_cannot_bypass_net_floor(goog_lab):
    from dataclasses import replace

    from lab.config import MarginSettings
    from lab.reservations import stressed_openings

    limits = replace(_limits("balanced"), max_weight=1, max_gross=2, max_short_gross=1, min_net=0, max_net=1.5)
    # Equal marked long and short values look neutral if all prices move together.
    # A -3% long / +3% short move gives negative net exposure, and must be refused.
    reason = stressed_openings(
        {"A": 5, "B": 0}, {"B": -5}, {"A": 100, "B": 100}, 1000, limits, MarginSettings(), goog_lab.costs, 0.03
    )
    assert reason and "net limit" in reason


def test_stress_bounds_cover_independent_fill_price_and_mark_corners(goog_lab):
    from dataclasses import replace
    from itertools import product

    from lab.config import MarginSettings
    from lab.reservations import stressed_openings

    limits = replace(_limits("balanced"), max_weight=0.8, max_gross=1.2, max_short_gross=0.6, min_net=-0.2, max_net=0.8)
    held, targets = {"A": 2, "B": -1}, {"A": 4, "B": -2}
    gap, price, equity = 0.03, 100, 1000
    margin = MarginSettings()
    assert (
        stressed_openings(held, targets, {"A": price, "B": price}, equity, limits, margin, goog_lab.costs, gap) is None
    )
    for fractions in product((0, 0.5, 1), repeat=2):
        for moves in product((-gap, gap), repeat=2):
            cash = equity - sum(held.values()) * price
            positions = {}
            for ticker, fraction in zip("AB", fractions, strict=True):
                delta = (targets[ticker] - held[ticker]) * fraction
                fill = price * (1 + gap if delta > 0 else 1 - gap)
                cash -= delta * fill + abs(delta) * fill * goog_lab.costs.commission_max_pct
                positions[ticker] = held[ticker] + delta
            values = [positions[t] * price * (1 + m) for t, m in zip("AB", moves, strict=True)]
            nav = cash + sum(values)
            weights = [v / nav for v in values]
            assert max(map(abs, weights)) <= limits.max_weight
            assert sum(map(abs, weights)) <= limits.max_gross
            assert -sum(min(w, 0) for w in weights) <= limits.max_short_gross
            assert limits.min_net <= sum(weights) <= limits.max_net
            assert sum(abs(w) * 0.5 for w in weights) * (1 + margin.cushion) <= 1
