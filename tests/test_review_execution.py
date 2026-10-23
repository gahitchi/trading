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
