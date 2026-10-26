"""The Gold migration attestation observes broker state without sending orders."""

import json

from lab.gold_migration import broker_attestation
from lab.signals.registry import SignalSpec
from lab.sim.clients import sim_clients
from lab.sim.harness import LiveHarness


def test_gold_migration_attestation_is_read_only(goog_lab, goog, tmp_path):
    harness = LiveHarness(
        goog_lab,
        [SignalSpec("buy_and_hold")],
        {"GOOG": goog},
        tmp_path / "harness",
        first_day=556,
    )
    broker = harness.broker()
    before = json.dumps(broker.state, sort_keys=True)
    plan = {"old": "identity-hash", "ledger": "ledger-hash"}

    report = broker_attestation(
        harness.cfg,
        ["GOOG"],
        plan,
        wait_s=1,
        clients=sim_clients(harness.market, broker, {}),
    )

    assert report["account"] == harness.cfg.paper.account_id
    assert report["open_orders"] == [] and report["positions"] == {}
    assert json.dumps(harness.broker().state, sort_keys=True) == before
