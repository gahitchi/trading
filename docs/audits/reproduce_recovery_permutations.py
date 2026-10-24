"""Exhaustive callback-order check supporting the release-hardening audit (no broker)."""

import itertools
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from lab.agents.execution import ExecutionAgent
from lab.execution_state import validate_ledger
from tests.test_disposal_review import _agent, _fill


def run():
    checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for disposed in (False, True):
            for events in itertools.permutations(("fill1", "fill2", "cancel", "recover", "restart", "duplicate")):
                a = _agent(root)
                if not disposed:
                    a.pending["X"] = a.disposed.pop("X")
                fills = [_fill("T1", 4), _fill("T2", 6)]
                observed = []
                order = SimpleNamespace(is_closed=False, events=lambda observed=observed: observed)
                a.orders["X"] = order
                for event in events:
                    if event.startswith("fill"):
                        fill = fills[int(event[-1]) - 1]
                        observed.append(fill)
                        if len(observed) == 2:
                            order.is_closed = True
                        ExecutionAgent.on_order_filled(a, fill)
                    elif event == "duplicate" and observed:
                        ExecutionAgent.on_order_filled(a, observed[0])
                    elif event == "cancel":
                        order.is_closed = True
                        ExecutionAgent.on_order_canceled(a, SimpleNamespace(client_order_id="X"))
                    elif event == "recover":
                        a._recover_pending()
                    elif event == "restart":
                        # Serialize exactly the accounting/attribution state, validate on restore,
                        # then make a fresh agent double with the broker's independently retained cache.
                        state = json.loads(
                            json.dumps(
                                {
                                    "version": 2,
                                    "cash": a.cash,
                                    "positions": a.held,
                                    "commissions": a.commissions,
                                    "account": a.ledger_account,
                                    "pending": a.pending,
                                    "disposed": a.disposed,
                                    "terminal": a.terminal,
                                    "applied_trades": [[t, ts, coid] for t, (ts, coid) in a.applied.items()],
                                }
                            )
                        )
                        validate_ledger(state)
                        a = _agent(root)
                        a.cash, a.held, a.commissions = state["cash"], state["positions"], state["commissions"]
                        a.pending, a.disposed, a.terminal = state["pending"], state["disposed"], state["terminal"]
                        a.applied = {t: [ts, coid] for t, ts, coid in state["applied_trades"]}
                        a.orders["X"] = order
                        a._recover_pending()
                a._recover_pending()
                assert a.held == {"A": 10} and a.cash == 900, (disposed, events, a.held, a.cash)
                assert set(a.applied) == {"T1", "T2"} and not a.pending, (disposed, events)
                assert a.terminal["X"]["filled"] == 10, (disposed, events)
                if disposed:
                    assert a.disposed["X"]["evidence"] == "checked"
                checked += 1
    return {
        "permutations": checked,
        "passed": True,
        "boundary": "real ExecutionAgent methods with a test-double broker cache and serialized state; not LiveNode or IBKR",
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
