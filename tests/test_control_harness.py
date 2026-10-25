"""Operator controls on the actual paper LiveNode with a simulated broker."""

import pytest

from lab import paper
from lab.authority import Authority
from lab.persistence import read_json
from lab.signals.registry import SignalSpec
from lab.sim.harness import LiveHarness

# Real-clock LiveNode runs against the simulated broker: the slow, serial tier (Gemini audit F-R19).
pytestmark = pytest.mark.harness


@pytest.mark.parametrize("mode", ["manual", "supervised"])
def test_controlled_node_does_not_submit_automatic_orders(mode, goog_lab, goog, tmp_path, monkeypatch):
    real_build = paper.build_node
    first = True

    def controlled(*args, **kwargs):
        nonlocal first
        node = real_build(*args, **kwargs, operator_control=True, mode=mode if first else None)
        first = False
        return node

    monkeypatch.setattr(paper, "build_node", controlled)
    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=556)
    result = h.session()
    assert result.error is None
    assert not h.broker().state["orders"] and not h.broker().positions()
    control = Authority(h.runtime, read_json(h.runtime / "identity.json"))
    if mode == "supervised":
        assert control.requests() and all(not r["approved"] for r in control.requests())
    else:
        control.issue("manual", targets={"GOOG": 0.2})
        result = h.session()
        assert result.error is None and h.broker().positions()["GOOG"] > 0
        assert h.ledger()["positions"] == h.broker().positions()


def test_runtime_identity_binds_execution_authority_limits(goog_lab, goog, tmp_path, monkeypatch):
    from dataclasses import replace

    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=556)
    h.session()
    identity = read_json(h.runtime / "identity.json")
    assert identity["execution"]["paper"]["max_orders_per_day"] == h.cfg.paper.max_orders_per_day
    changed = replace(h.cfg, paper=replace(h.cfg.paper, max_orders_per_day=h.cfg.paper.max_orders_per_day + 1))
    with pytest.raises(ValueError, match="runtime identity changed"):
        paper.build_node(changed, h.specs, h.tickers)
