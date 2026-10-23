"""Order-transition regressions from the follow-up audit, using the real rebalance method."""

from types import SimpleNamespace

from lab.agents.execution import ExecutionAgent


def _agent(held, weights, max_gross=None):
    sent = []
    cfg = SimpleNamespace(
        max_gross=max_gross,
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
