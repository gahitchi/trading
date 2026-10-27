"""The Gold migration attestation observes broker state without sending orders."""

import json

from lab.authority import digest
from lab.gold_migration import broker_attestation, enable_gold
from lab.paper import runtime_dir
from lab.persistence import atomic_json
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


def test_completed_gold_migration_can_finish_saving_after_a_crash(goog_lab, tmp_path):
    rt = runtime_dir(goog_lab)
    identity = {"account": goog_lab.paper.account_id, "gold": "london-spot-atr-v1"}
    current = digest(identity)
    backup = tmp_path / "private-backup"
    atomic_json(backup / "backup-manifest.json", {"version": 1})
    receipt = {
        "action": "enable-gold",
        "new": current,
        "backup": {"destination": str(backup)},
    }
    atomic_json(rt / "identity.json", identity)
    atomic_json(rt / f"upgrade-{current}.json", receipt)

    assert enable_gold(goog_lab, ["GOOG"], tmp_path / "unused", "app:test") == receipt
